// SPDX-FileCopyrightText: 2026 José Goudet Alvim
// SPDX-License-Identifier: GPL-3.0-or-later

#include <string.h>
#include <stdlib.h>
#include <stddef.h>
#include <stdio.h>
#include <fcntl.h>           /* Definition of O_* constants */
#include <unistd.h>
#include <sys/stat.h>
#include <errno.h>
#include <stdint.h>

#define pushback(_v, x) ({\
	__auto_type v = &(_v); \
	if (v->count >= v->capacity) { \
		size_t new_capacity = v->capacity > 0 ? 2 * v->capacity : 16; \
		size_t bytes = new_capacity * sizeof(v->items[0]); \
		void* buffer = realloc(v->items, bytes); \
		if (buffer == NULL) { \
			perror("depi: realloc"); \
			exit(EXIT_FAILURE); \
		} \
		v->items = buffer; \
		v->capacity = new_capacity; \
	} \
	v->items[v->count++] = (x);\
})

#define RESOLVE_CACHE_BUCKETS     256
#define RESOLVE_CACHE_MAX_ENTRIES 1024*1024

struct include {
	char* path;
	int   fd;
	int   is_system;   /* leaf node for recursion */
};

struct settings {
	struct {
		size_t count, capacity;
		struct include* items;
	} includes;

	struct file_paths {
		size_t count, capacity;
		char** items; 
	} target_files;
};

struct file_id {
	dev_t dev;
	ino_t ino;
};

struct work_list {
	size_t count, capacity;
	char** items;
};

struct dependency {
	char* name;
	int quoted;
};

struct file_set_entry {
	struct file_id id;
	int used;
};

struct file_set {
	size_t count;      /* occupied entries */
	size_t capacity;   /* slots, always a power of two */
	struct file_set_entry* entries;
};

/* Fixed-size chained cache. Keys are include names, copied because the
 * per-file names are freed after each file. Once full, new entries are
 * not inserted; cached entries remain valid. Name-only keys are valid
 * for the global include search list; quoted includes resolved next to
 * the including file bypass the cache. Positive entries also keep the
 * resolved file identity so a cache hit can be queued without another
 * stat. */
struct resolve_cache_entry {
	uint64_t hash;
	char*    name;
	struct file_id id;
	int      index;   /* include index, -1 = not found */
	int      next;    /* next entry in bucket, -1 = end */
};

struct resolve_cache {
	struct resolve_cache_entry entries[RESOLVE_CACHE_MAX_ENTRIES];
	int buckets[RESOLVE_CACHE_BUCKETS];
	size_t count;
};

struct context {
	struct settings const* settings;
	struct resolve_cache* cache;
	struct work_list work;
	struct file_set visited;
};

/* SplitMix64-style finalizer. SplitMix64 is public domain (CC0); see
 * Sebastiano Vigna's reference implementation. */
static uint64_t hash(uint64_t seed, uint64_t data) {
	uint64_t v = data + seed * 0x9e3779b97f4a7c15;
	v = (v ^ (v >> 30)) * 0xbf58476d1ce4e5b9;
	v = (v ^ (v >> 27)) * 0x94d049bb133111eb;
	return v ^ (v >> 31);
}

static uint64_t hash_string(char const* string) {
	uint64_t seed = 0;
	for (; *string != '\0'; string++)
		seed = hash(seed, (uint64_t)(unsigned char)*string);
	return seed;
}

static void cache_init(struct resolve_cache* cache) {
	for (size_t i = 0; i < RESOLVE_CACHE_BUCKETS; i++)
		cache->buckets[i] = -1;
	cache->count = 0;
}

static struct resolve_cache_entry* cache_find(
	struct resolve_cache* cache, char const* name, uint64_t h
) {
	size_t bucket = (size_t)(h % RESOLVE_CACHE_BUCKETS);
	for (int i = cache->buckets[bucket]; i != -1; i = cache->entries[i].next) {
		struct resolve_cache_entry* entry = &cache->entries[i];
		if (entry->hash == h && 0 == strcmp(entry->name, name))
			return entry;
	}
	return NULL;
}

static void cache_store(
	struct resolve_cache* cache, char const* name, uint64_t h,
	int index, struct file_id id
) {
	if (cache->count >= (size_t)RESOLVE_CACHE_MAX_ENTRIES)
		return;

	char* copy = strdup(name);
	if (copy == NULL) {
		perror("depi: strdup");
		exit(EXIT_FAILURE);
	}

	size_t slot = cache->count++;
	size_t bucket = (size_t)(h % RESOLVE_CACHE_BUCKETS);
	struct resolve_cache_entry* entry = &cache->entries[slot];
	entry->hash = h;
	entry->name = copy;
	entry->id = id;
	entry->index = index;
	entry->next = cache->buckets[bucket];
	cache->buckets[bucket] = (int)slot;
}

static void cache_free(struct resolve_cache* cache) {
	for (size_t i = 0; i < cache->count; i++)
		free(cache->entries[i].name);
}

static uint64_t file_id_hash(struct file_id id) {
	return hash((uint64_t)id.dev, (uint64_t)id.ino);
}

static void file_set_grow(struct file_set* set) {
	size_t new_capacity = set->capacity > 0 ? 2 * set->capacity : 64;
	struct file_set_entry* entries = calloc(new_capacity, sizeof(entries[0]));
	if (entries == NULL) {
		perror("depi: calloc");
		exit(EXIT_FAILURE);
	}

	size_t mask = new_capacity - 1;
	for (size_t i = 0; i < set->capacity; i++) {
		if (!set->entries[i].used)
			continue;

		size_t slot = (size_t)file_id_hash(set->entries[i].id) & mask;
		while (entries[slot].used)
			slot = (slot + 1) & mask;
		entries[slot] = set->entries[i];
	}

	free(set->entries);
	set->entries = entries;
	set->capacity = new_capacity;
}

static int file_set_add(struct file_set* set, struct file_id id) {
	if (set->capacity == 0)
		file_set_grow(set);

	size_t mask = set->capacity - 1;
	size_t slot = (size_t)file_id_hash(id) & mask;
	while (set->entries[slot].used) {
		if (set->entries[slot].id.dev == id.dev &&
		    set->entries[slot].id.ino == id.ino)
			return 0;
		slot = (slot + 1) & mask;
	}

	if (set->count + 1 > set->capacity - set->capacity / 4) {
		file_set_grow(set);
		mask = set->capacity - 1;
		slot = (size_t)file_id_hash(id) & mask;
		while (set->entries[slot].used)
			slot = (slot + 1) & mask;
	}

	set->entries[slot].id = id;
	set->entries[slot].used = 1;
	set->count++;
	return 1;
}

static void file_set_free(struct file_set* set) {
	free(set->entries);
}

static void work_free(struct work_list* work) {
	for (size_t i = 0; i < work->count; i++)
		free(work->items[i]);
	free(work->items);
}

static int contains(char* const* items, size_t count, char const* value) {
	for (size_t i = 0; i < count; i++) {
		if (0 == strcmp(items[i], value))
			return 1;
	}
	return 0;
}

static int dependency_contains(
	struct dependency const* items, size_t count,
	char const* name, int quoted
) {
	for (size_t i = 0; i < count; i++) {
		if (items[i].quoted == quoted && 0 == strcmp(items[i].name, name))
			return 1;
	}
	return 0;
}

static int find_include_path(
	struct include const* items, size_t count, char const* path
) {
	for (size_t i = 0; i < count; i++) {
		if (0 == strcmp(items[i].path, path))
			return (int)i;
	}
	return -1;
}

void print_help(FILE* of) {
	fprintf(of, "usage: depi [-h] <-I=path|-S=path>* <file|->+\n");
	fprintf(of, "  -I=path  add an include search directory (options must precede files)\n");
	fprintf(of, "  -S=path  add a system include directory (resolved dependencies are leaves)\n");
	fprintf(of, "  -        read file paths from stdin, one per line\n");
	fprintf(of, "  -h       show this help\n");
}

static char* dir_of_path(char const* path) {
	char const* slash = strrchr(path, '/');
	size_t len = 0;

	if (slash != NULL)
		len = slash == path ? 1 : (size_t)(slash - path);

	char* dir = malloc(len + 1);
	if (dir == NULL) {
		perror("depi: malloc");
		exit(EXIT_FAILURE);
	}
	if (len > 0)
		memcpy(dir, path, len);
	dir[len] = '\0';
	return dir;
}

static char* join_path(char const* dir, char const* name) {
	if (name[0] == '/') {
		char* path = strdup(name);
		if (path == NULL) {
			perror("depi: strdup");
			exit(EXIT_FAILURE);
		}
		return path;
	}

	size_t dir_len = strlen(dir);
	size_t name_len = strlen(name);
	int add_slash = dir_len > 0 && dir[dir_len - 1] != '/';
	size_t total = dir_len + (size_t)add_slash + name_len + 1;
	char* path = malloc(total);
	if (path == NULL) {
		perror("depi: malloc");
		exit(EXIT_FAILURE);
	}

	if (dir_len > 0)
		memcpy(path, dir, dir_len);
	size_t pos = dir_len;
	if (add_slash)
		path[pos++] = '/';
	memcpy(path + pos, name, name_len + 1);
	return path;
}

static char* parse_include(char const* line, int* quoted) {
	char* name = NULL;
	int end = -1;
	int matched = sscanf(line, " # include <%m[^>]>%n", &name, &end);

	if (matched == 1 && name != NULL && end >= 0) {
		*quoted = 0;
		return name;
	}
	free(name);

	name = NULL;
	end = -1;
	matched = sscanf(line, " # include \"%m[^\"]\"%n", &name, &end);
	if (matched == 1 && name != NULL && end >= 0) {
		*quoted = 1;
		return name;
	}
	free(name);
	return NULL;
}

static void settings_free(struct settings* settings) {
	for (size_t i = 0; i < settings->includes.count; i++) {
		struct include* inc = &settings->includes.items[i];
		if (inc->fd >= 0)
			close(inc->fd);
		free(inc->path);
	}
	free(settings->includes.items);

	for (size_t i = 0; i < settings->target_files.count; i++)
		free(settings->target_files.items[i]);
	free(settings->target_files.items);
}

static int target_add(struct settings* settings, char const* path, int dedup) {
	if (dedup && contains(
		settings->target_files.items, settings->target_files.count, path
	))
		return 0;

	char* copy = strdup(path);
	if (copy == NULL) {
		perror("depi: strdup");
		return -1;
	}
	pushback(settings->target_files, copy);
	return 0;
}

static int read_targets_stdin(struct settings* settings) {
	char* line = NULL;
	size_t cap = 0;
	ssize_t nread;
	int status = 0;

	while ((nread = getline(&line, &cap, stdin)) != -1) {
		if (nread > 0 && line[nread - 1] == '\n') {
			line[nread - 1] = '\0';
			nread--;
		}
		if (nread == 0)
			continue;

		if (target_add(settings, line, 0) < 0) {
			status = -1;
			break;
		}
	}

	if (ferror(stdin)) {
		fprintf(stderr, "depi: stdin: %s\n", strerror(errno));
		status = -1;
	}
	free(line);
	return status;
}

int parse_settings(int argc, char** argv, struct settings* settings) {
	if (argc <= 1) {
		print_help(stderr);
		return -1;
	}
	
	int i = 1;
	for (; i < argc; i++) {
		char const* value;
		int is_system;

		if (0 == strncmp(argv[i], "-I=", 3)) {
			value = argv[i] + 3;
			is_system = 0;
		} else if (0 == strncmp(argv[i], "-S=", 3)) {
			value = argv[i] + 3;
			is_system = 1;
		} else {
			break;
		}

		if ('\0' == *value) {
			fprintf(stderr, "depi: empty include path\n");
			return -1;
		}

		int existing = find_include_path(
			settings->includes.items, settings->includes.count, value
		);
		if (existing >= 0) {
			if (is_system)
				settings->includes.items[existing].is_system = 1;
			continue;
		}

		char* copy = strdup(value);
		if (copy == NULL) {
			perror("depi: strdup");
			return -1;
		}

		struct include inc = {
			.path = copy,
			.fd = -1,
			.is_system = is_system,
		};
		pushback(settings->includes, inc);
	}
	
	int stdin_seen = 0;
	for (; i < argc; i++) {
		if (0 == strcmp(argv[i], "-")) {
			if (stdin_seen)
				continue;
			stdin_seen = 1;
			if (read_targets_stdin(settings) < 0)
				return -1;
			continue;
		}

		if (target_add(settings, argv[i], 1) < 0)
			return -1;
	} 

	if (settings->target_files.count == 0) {
		fprintf(stderr, "depi: no input files\n");
		print_help(stderr);
		return -1;
	}

	return 0;
}

static void work_enqueue(struct context* ctx, char* path, struct file_id id) {
	if (!file_set_add(&ctx->visited, id)) {
		free(path);
		return;
	}
	pushback(ctx->work, path);
}

static void emit_resolved(
	FILE* of, struct context* ctx, char* path,
	struct file_id id, int is_system
) {
	fprintf(of, "\"%s\" ", path);
	if (is_system)
		free(path);
	else
		work_enqueue(ctx, path, id);
}

static int work_add_target(struct context* ctx, char const* path) {
	struct stat sb;
	if (stat(path, &sb) != 0) {
		fprintf(stderr, "depi: '%s': %s\n", path, strerror(errno));
		return -1;
	}
	if (!S_ISREG(sb.st_mode)) {
		fprintf(stderr, "depi: '%s': not a regular file\n", path);
		return -1;
	}

	struct file_id id = { .dev = sb.st_dev, .ino = sb.st_ino };
	if (!file_set_add(&ctx->visited, id))
		return 0;

	char* copy = strdup(path);
	if (copy == NULL) {
		perror("depi: strdup");
		exit(EXIT_FAILURE);
	}
	pushback(ctx->work, copy);
	return 0;
}

int do_file_line(FILE* of, char const* path, struct context* ctx) {
	FILE* file = fopen(path, "r");
	ssize_t nread;
	size_t line_cap = 0;
	char* line = NULL;
	char* source_dir = NULL;
	int status = 0;
	struct {
		size_t count, capacity;
		struct dependency* items;
	} dependencies = {0};

	struct include const* inc = ctx->settings->includes.items;
	size_t nincs = ctx->settings->includes.count;
	struct resolve_cache* cache = ctx->cache;

	if (file == NULL) {
		fprintf(stderr, "depi: '%s': %s\n", path, strerror(errno));
		return -1;
	} 

	source_dir = dir_of_path(path);

	while ((nread = getline(&line, &line_cap, file)) != -1) {
		int quoted = 0;
		char* name = parse_include(line, &quoted);
		if (name == NULL)
			continue;

		if (!dependency_contains(
			dependencies.items, dependencies.count, name, quoted
		)) {
			struct dependency dep = {
				.name = name,
				.quoted = quoted,
			};
			pushback(dependencies, dep);
		} else {
			free(name);
		}
	}

	if (ferror(file)) {
		fprintf(stderr, "depi: '%s': read error\n", path);
		status = -1;
	}

	fprintf(of, "%s: ", path);
	for (size_t i = 0; i < dependencies.count; i++) {
		struct dependency const* dep = &dependencies.items[i];
		char const* included = dep->name;
		struct file_id id = {0};

		if (dep->quoted) {
			char* local_path = join_path(source_dir, included);
			struct stat sb;
			if (stat(local_path, &sb) == 0) {
				if (S_ISREG(sb.st_mode)) {
					id.dev = sb.st_dev;
					id.ino = sb.st_ino;
					emit_resolved(of, ctx, local_path, id, 0);
					continue;
				}
				free(local_path);
			} else {
				int err = errno;
				if (err != ENOENT && err != ENOTDIR) {
					fprintf(stderr, "depi: %s: %s\n",
						local_path, strerror(err));
				}
				free(local_path);
			}
		}

		uint64_t h = hash_string(included);
		struct resolve_cache_entry* cached = cache_find(cache, included, h);
		int resolved;

		if (cached != NULL) {
			resolved = cached->index;
			id = cached->id;
		} else {
			resolved = -1;
			for (size_t j = 0; j < nincs; j++) {
				if (inc[j].fd < 0)
					continue;

				struct stat sb;
				if (fstatat(inc[j].fd, included, &sb, 0) == 0) {
					if (!S_ISREG(sb.st_mode))
						continue;

					resolved = (int)j;
					id.dev = sb.st_dev;
					id.ino = sb.st_ino;
					break;
				}

				int err = errno;
				if (err != ENOENT && err != ENOTDIR) {
					fprintf(stderr, "depi: %s: %s: %s\n",
						inc[j].path, included, strerror(err));
				}
			}
			cache_store(cache, included, h, resolved, id);
		}

		if (resolved < 0) {
			if (dep->quoted)
				fprintf(stderr, "depi: '%s': include \"%s\" not found\n",
					path, included);
			else
				fprintf(stderr, "depi: '%s': include <%s> not found\n",
					path, included);
			continue;
		}

		emit_resolved(
			of, ctx, join_path(inc[resolved].path, included),
			id, inc[resolved].is_system
		);
	}
	fprintf(of, "\n");	

	if (fclose(file) != 0) {
		fprintf(stderr, "depi: '%s': close: %s\n", path, strerror(errno));
		status = -1;
	}
	free(source_dir);
	free(line);
	for (size_t i = 0; i < dependencies.count; i++)
		free(dependencies.items[i].name);
	free(dependencies.items);

	return status;
}

int main(int argc, char** argv) {
	struct settings settings = {0};
	int failed = 0;

	if (argc >= 2 && 0 == strcmp(argv[1], "-h")) {
		print_help(stdout);
		return EXIT_SUCCESS;
	}

	if (parse_settings(argc, argv, &settings) < 0) {
		settings_free(&settings);
		return EXIT_FAILURE;
	}

	/* The fixed table can be large (1 << 20 entries), so keep it off the stack. */
	struct resolve_cache* cache = malloc(sizeof *cache);
	if (cache == NULL) {
		perror("depi: malloc");
		settings_free(&settings);
		return EXIT_FAILURE;
	}
	cache_init(cache);

	for (size_t i = 0; i < settings.includes.count; i++) {
		struct include* inc = &settings.includes.items[i];
		inc->fd = open(inc->path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
		if (inc->fd == -1) {
			fprintf(stderr, "depi: include directory '%s': %s\n",
				inc->path, strerror(errno));
			failed = 1;
		}
	}

	struct context ctx = {0};
	ctx.settings = &settings;
	ctx.cache = cache;

	for (size_t i = 0; i < settings.target_files.count; i++) {
		if (work_add_target(&ctx, settings.target_files.items[i]) < 0)
			failed = 1;
	}

	for (size_t i = 0; i < ctx.work.count; i++) {
		char* path = ctx.work.items[i];
		if (do_file_line(stdout, path, &ctx) < 0)
			failed = 1;
	}

	if (fflush(stdout) != 0) {
		fprintf(stderr, "depi: stdout: %s\n", strerror(errno));
		failed = 1;
	}

	work_free(&ctx.work);
	file_set_free(&ctx.visited);
	cache_free(cache);
	free(cache);
	settings_free(&settings);
	return failed ? EXIT_FAILURE : EXIT_SUCCESS;
}
