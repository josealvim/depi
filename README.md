# depi

`depi` scans C source and header files and emits a Make dependency fragment. For
each root source it resolves angle-bracket and quoted includes recursively, then
prints an object rule containing the transitive header closure followed by empty
rules for the headers.

## Build

```sh
./build.sh
```

The build uses `gcc` and requires a C23 compiler with GNU extensions
(statement expressions, `__auto_type`, `%m` scanf allocation), a POSIX/Linux
libc (`getline`, `strdup`, `fstatat`), and `O_DIRECTORY` support.

## Usage

```text
depi [-h] [-q] <-I=path|-S=path>* --map=pattern=replacement <file|->+
```

- `-I=path` — add an include search directory.
- `-S=path` — add a system include directory; dependencies resolved there are
  omitted from the generated dependency graph.
- `--map=pattern=replacement` — map root source paths to object paths.
  Repeatable; required for every root.
- `-q` — suppress warnings.
- `-` — read source paths from stdin, one per line.
- `-h` — print help.

Options must precede input files. The `=` is part of the option syntax.

### Path mapping

Every root must match a map. There is no default object naming.

Pattern matching:

- A pattern may contain zero or one `%`.
- Zero `%` means an exact source-path match.
- One `%` matches any sequence of characters, including `/`.
- More than one `%` in the pattern is an error.

Replacement:

- With a captured `%`, every `%` in the replacement is replaced by the capture.
- A replacement without `%` is used literally.
- Empty patterns and empty replacements are errors.
- Multiple maps are tried in order; the first match wins.
- No match for a root is fatal.
- Patterns match source paths exactly as supplied; `./src/foo.c` does not
  match `src/%.c`.
- Duplicate object targets are not detected; that is the user's responsibility.

Examples:

```sh
# src/foo.c -> build/foo.o; src/sub/foo.c -> build/sub/foo.o
--map='src/%.c=build/%.o'

# src/foo.c -> build/src/foo.o
--map='%.c=build/%.o'

# exact mapping
--map='src/foo.c=build/special.o'
```

The generated object paths must match the same object paths used by the
Makefile's build rules.

### Reading paths from stdin

```sh
find src -type f -name '*.c' \
    | ./depi -I=include --map='src/%.c=build/%.o' -
```

Stdin is read at most once and can be mixed with ordinary file operands. Paths
are preserved except for the trailing newline. Use `find -type f`; non-regular
targets are errors. A literal file named `-` is addressed as `./-`.

## Output

For a source tree where `src/main.c` includes `src/a.h`, `include/b.h`, and
`include/c.h`:

```make
build/main.o: src/main.c src/a.h include/b.h include/c.h
src/a.h:
include/b.h:
include/c.h:
```

The closure is transitive. Repeated dependencies and cycles are emitted once.

## Makefile integration

### Usage

To use depi from a Makefile:

1. Define `DEPI`, its include flags, and at least one `--map=` rule. The mapped
   object paths must be exactly the paths used by `$(OBJS)` and the compile
   pattern rule.
2. Feed the source list to depi on stdin using `-`.
3. Generate one dependency fragment from a rule that is forced on every Make
   invocation.
4. Include that fragment with `-include` after the default goal.
5. Use the generated object paths in the compile rules.

At the command level, one invocation looks like:

```sh
printf '%s\n' src/main.c \
    | ./depi -I=include --map='src/%.c=build/%.o' -
```

The complete Makefile pattern is:

```make
DEPI       := ./depi
DEPI_FLAGS := -I=include
MAP        := --map='src/%.c=build/%.o'
SRCS       := $(wildcard src/*.c)
OBJS       := $(patsubst src/%.c,build/%.o,$(SRCS))
DEPFILE    := .depi.mk

all: app

app: $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $^

build/%.o: src/%.c | build
	$(CC) $(CFLAGS) -c $< -o $@

build:
	mkdir -p $@

$(DEPFILE): $(DEPI) FORCE
	@tmp=$@.$$$$; \
	printf '%s\n' $(SRCS) | \
	    $(DEPI) $(MAP) $(DEPI_FLAGS) - > $$tmp && mv $$tmp $@

-include $(DEPFILE)

.PHONY: FORCE
FORCE:
```

`FORCE` regenerates the fragment on every Make invocation, so removed headers
and changed include lists cannot leave stale dependencies behind.

## Behavior

- Include directories are searched in the order given. The first directory
  containing a matching regular file wins.
- Quoted includes (`#include "file.h"`) search the directory containing the
  including file first, then the configured `-I`/`-S` directories. Angle-bracket
  includes (`#include <file.h>`) search only the configured directories.
- Dependencies resolved through `-S` are not added to the graph and produce no
  rules or stubs.
- Each root gets one object rule. Root aliases are deduplicated by device/inode,
  so a hard link or symlink alias produces only one rule.
- Unresolved quoted includes are fatal. Unresolved angle-bracket includes are
  warnings and can be suppressed with `-q`. This distinction is heuristic: see
  the limitations below.
- Missing project include directories are fatal; missing system include
  directories are warnings.
- Whitespace is allowed between `#` and `include`, and leading whitespace
  before `#` is accepted.
- Generated Make paths escape spaces, `#`, `$`, and backslashes.

### Limitations

- There is no reliable way to classify an unresolved include as project or
  system once no include directory contains it. The current rule uses the
  include syntax as a heuristic: quoted includes are treated as project
  dependencies and are fatal when unresolved; angle-bracket includes are
  treated as system dependencies and only produce a warning. Consequently, a
  missing project header written as `#include <foo.h>` is not fatal, while a
  missing system header written as `#include "foo.h"` is fatal. `-S` paths
  cannot resolve this ambiguity because an unresolved name never matches a
  path.
- Macro includes and unterminated header names are ignored.
- The scanner is line-based and does not evaluate comments, strings,
  conditionals, or line continuations; an include inside a block comment or
  inactive `#if` branch can still be reported.
- Include names are resolved as given; `..` and symlinks are left to the OS.
- Stdin paths are newline-separated; filenames containing newlines are not
  supported, and NUL-separated `find -print0` input is not supported yet.
- Duplicate object mappings are not diagnosed.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

Copyright (C) 2026 José Goudet Alvim.

The SplitMix64 finalizer is public domain (CC0).
