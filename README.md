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

## Tests

```sh
./tests/run.sh
```

The suite always rebuilds `depi`, then runs the Python 3 `unittest` tests. It
covers CLI parsing, include resolution, mapping, escaping, stdin input, depfile
output, and the Make integration. The Make integration tests look for GNU Make
(`gmake` or `make`) and are skipped when it is unavailable. The documented
Makefile itself uses POSIX.1-2024 constructs.

## Usage

```text
depi [-h] [-q] <-I=path|-S=path>* --map=pattern=replacement [--depfile=path] <file|->+
```

- `-I=path` — add an include search directory.
- `-S=path` — add a system include directory; dependencies resolved there are
  omitted from the generated dependency graph.
- `--map=pattern=replacement` — map root source paths to object paths.
  Repeatable; required for every root.
- `--depfile=path` — emit a self-dependency rule for the generated fragment.
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
src/main.o: src/main.c src/a.h include/b.h include/c.h
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
   rules.
2. Feed the source list to depi on stdin using `-`.
3. Generate one dependency fragment with `--depfile=...` so the fragment
   depends on every source and header it scans.
4. Include that fragment with `include` after the rule that creates it.
5. Use the generated object paths in the compile rules.

At the command level, one invocation looks like:

```sh
printf '%s\n' src/main.c src/util.c \
    | ./depi -I=include --map='%.c=%.o' -
```

A POSIX.1-2024 Makefile using depi is:

```make
.POSIX:
DEPI = ./depi
DEPI_FLAGS = -I=include
CFLAGS += -Iinclude
MAP = --map='%.c=%.o'
SRCS = src/main.c src/util.c
OBJS = $(SRCS:.c=.o)
DEPFILE = .depi.mk

all: app

app: $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $(OBJS)

.c.o:
	$(CC) $(CFLAGS) -c $< -o $@

$(DEPFILE): $(SRCS) $(DEPI)
	@tmp="$@.$$$$"; \
	trap 'rm -f "$$tmp"' 0; \
	trap 'exit 1' 1 2 15; \
	printf '%s\n' $(SRCS) | \
	    $(DEPI) --depfile="$@" $(MAP) $(DEPI_FLAGS) - > "$$tmp" && \
	mv "$$tmp" "$@"

include $(DEPFILE)
```

This example keeps object files next to the sources so that the portable
`.c.o` suffix rule can build them. POSIX make has no `%.o: %.c` pattern rules.
The compiler include path in `CFLAGS` must mirror the directories passed to
depi in `DEPI_FLAGS`.

To use a separate build directory, set `MAP` to
`--map='src/%.c=build/%.o'`, set `OBJS` to the corresponding paths, and write
one explicit rule per source, for example:

```make
build/main.o: src/main.c
	@mkdir -p $(@D)
	$(CC) $(CFLAGS) -c $< -o $@
```

The `.POSIX:` line requests the standard defaults. With GNU Make that means
`CC=c99` and `CFLAGS=-O1`; override them in the environment or on the command
line if desired.

depi emits a rule such as:

```make
.depi.mk: src/main.c src/util.c src/a.h include/b.h
```

so Make regenerates the fragment when any scanned source or header changes.
`include` is intentional: `-include` would silently ignore a depi failure.
Because the depfile rule appears before the include line, the first invocation
creates `.depi.mk` before it is read, and later invocations regenerate it from
the self-dependency.

This relies on the POSIX.1-2024 rule that missing or out-of-date included
makefiles are rebuilt. GNU Make supports it. POSIX.1-2017 made no such
guarantee; on older makes, generate `.depi.mk` before invoking make or use GNU
Make. BSD make uses a different `.include` syntax and is not covered by this
example.

Do not add a phony prerequisite such as `FORCE` or `always` to an included
makefile: Make will remake it, restart, see it is still out of date, and repeat
forever. The trap removes the temporary file on failure and on HUP/INT/TERM.

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
- With `--depfile=path`, the output ends with a self-dependency rule for that
  path, covering every scanned source and non-system header. Makefiles should
  use this instead of forcing an included depfile with a phony target.

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
