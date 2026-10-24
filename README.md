# depi

`depi` prints the include dependencies of C source and header files. Given a set
of include directories, it resolves angle-bracket and quoted includes and emits
one `file: "path/header" ...` line per processed file. Dependencies resolved
under `-I` directories are processed recursively; dependencies resolved under
`-S` directories are printed but treated as leaves.

## Build

```sh
./build.sh
```

The build uses `gcc` and requires a C23 compiler with GNU extensions
(statement expressions, `__auto_type`, `%m` scanf allocation), a POSIX/Linux
libc (`getline`, `strdup`, `fstatat`), and `O_DIRECTORY` support.

## Usage

```text
depi [-h] <-I=path|-S=path>* <file>+
```

- `-I=path` — add an include search directory; its resolved dependencies are
  traversed recursively.
- `-S=path` — add a system include search directory; its resolved dependencies
  are printed but not traversed.
- `-h` — print help.

Options must precede the input files. The `=` is part of the option syntax.

Examples:

```sh
./depi -I=include src/main.c
./depi -I=include -S=/usr/include src/main.c src/other.c
```

Example output:

```text
src/main.c: "include/foo.h" "include/bar.h"
include/foo.h: "include/baz.h"
```

## Behavior

- Include directories are searched in the order given. The first directory
  containing a matching regular file wins.
- Quoted includes (`#include "file.h"`) search the directory containing the
  including file first, then the configured `-I`/`-S` directories. Angle-bracket
  includes (`#include <file.h>`) search only the configured directories.
- Whitespace is allowed between `#` and `include`, and leading whitespace
  before `#` is accepted.
- Include directories, input files, and repeated includes within one file are
  deduplicated. Angle-bracket and quoted forms of the same name are tracked
  separately.
- Each input or resolved file is processed once. Files are identified by
  device and inode, so cycles, aliases, and repeated includes terminate.
- Traversal is breadth-first; every processed file gets its own line.
- A quoted include found next to the including file is traversed normally. If
  it falls back to a `-S` directory, it is a leaf.
- Unresolved includes are reported on stderr but do not make the run fail.
  Missing or non-regular input files, unreadable include directories, and I/O
  errors produce a nonzero exit status.
- No system include directories are assumed; pass them explicitly with `-I=`
  (recursive) or `-S=` (leaf).

### Limitations

- Macro includes and unterminated header names are ignored.
- The scanner is line-based and does not evaluate comments, strings,
  conditionals, or line continuations; an include inside a block comment or
  inactive `#if` branch can still be reported.
- Include names are resolved as given; `..` and symlinks are left to the OS.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

Copyright (C) 2026 depi contributors.

The SplitMix64 finalizer is public domain (CC0).
