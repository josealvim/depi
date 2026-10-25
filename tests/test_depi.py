#!/usr/bin/env python3
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPI = str(ROOT / "depi")


def find_gnu_make():
    for name in ("gmake", "make"):
        path = shutil.which(name)
        if path is None:
            continue
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "GNU Make" in result.stdout:
            return path
    return None


MAKE = find_gnu_make()


def lines(*items):
    return "".join(item + "\n" for item in items)


class DepiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, rel, content=""):
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def run_depi(self, *args, input=None, timeout=20):
        return subprocess.run(
            [DEPI, *[str(arg) for arg in args]],
            cwd=self.dir,
            input=input,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def ok(self, *args, input=None):
        result = self.run_depi(*args, input=input)
        self.assertEqual(result.returncode, 0, f"stderr:\n{result.stderr}")
        return result

    def expect_failure(self, *args, input=None):
        result = self.run_depi(*args, input=input)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        return result


class CliTests(DepiTestCase):
    def test_help(self):
        result = self.run_depi("-h")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage: depi", result.stdout)
        self.assertIn("--map", result.stdout)
        self.assertIn("--depfile", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_no_args(self):
        result = self.run_depi()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage: depi", result.stderr)

    def test_no_input_files(self):
        result = self.run_depi("-I=inc")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("no input files", result.stderr)

    def test_options_after_files_are_treated_as_inputs(self):
        self.write("src/main.c")
        result = self.run_depi(
            "--map=src/%.c=build/%.o", "src/main.c", "-q"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("'-q'", result.stderr)


class ParsingTests(DepiTestCase):
    def test_include_forms_and_ignored_lines(self):
        headers = [f"h{i}.h" for i in range(1, 7)]
        for header in headers:
            self.write(f"inc/{header}")

        self.write(
            "src/main.c",
            "\n".join([
                "#include <h1.h>",
                "#\tinclude\t\"h2.h\"",
                "# include <h3.h>",
                "   #   include   <h4.h>",
                "#include<h5.h>",
                "#include\"h6.h\"",
                "#include <h1.h>",
                "#include MACRO",
                "#include <unterminated.h",
                "#include \"unterminated.h",
                "#include_next <nope.h>",
                "#include <>",
                "#include \"\"",
            ]) + "\n",
        )

        result = self.ok(
            "-I=inc", "--map=src/%.c=build/%.o", "src/main.c"
        )
        expected = lines(
            "build/main.o: src/main.c inc/h1.h inc/h2.h inc/h3.h "
            "inc/h4.h inc/h5.h inc/h6.h",
            *[f"inc/{header}:" for header in headers],
        )
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")

    def test_angle_and_quoted_forms_are_separate(self):
        self.write("src/pick.h")
        self.write("inc/pick.h")
        self.write(
            "src/main.c",
            "#include <pick.h>\n"
            "#include <pick.h>\n"
            "#include \"pick.h\"\n"
            "#include \"pick.h\"\n",
        )

        result = self.ok(
            "-I=inc", "--map=src/%.c=build/%.o", "src/main.c"
        )
        expected = lines(
            "build/main.o: src/main.c inc/pick.h src/pick.h",
            "inc/pick.h:",
            "src/pick.h:",
        )
        self.assertEqual(result.stdout, expected)


class ResolutionTests(DepiTestCase):
    def test_include_path_order(self):
        self.write("inc1/pick.h")
        self.write("inc2/pick.h")
        self.write("src/main.c", "#include <pick.h>\n")
        common = ("--map=src/%.c=build/%.o", "src/main.c")

        first = self.ok("-I=inc1", "-I=inc2", *common)
        self.assertEqual(
            first.stdout,
            lines(
                "build/main.o: src/main.c inc1/pick.h",
                "inc1/pick.h:",
            ),
        )

        second = self.ok("-I=inc2", "-I=inc1", *common)
        self.assertEqual(
            second.stdout,
            lines(
                "build/main.o: src/main.c inc2/pick.h",
                "inc2/pick.h:",
            ),
        )

    def test_angle_does_not_use_local_file(self):
        self.write("src/pick.h")
        self.write("src/main.c", "#include <pick.h>\n")

        result = self.ok("--map=src/%.c=build/%.o", "src/main.c")
        self.assertEqual(result.stdout, "build/main.o: src/main.c\n")
        self.assertIn("include <pick.h> not found", result.stderr)

    def test_directory_candidate_is_not_a_header(self):
        (self.dir / "inc" / "dir.h").mkdir(parents=True)
        self.write("src/main.c", "#include <dir.h>\n")

        result = self.ok(
            "-I=inc", "--map=src/%.c=build/%.o", "src/main.c"
        )
        self.assertEqual(result.stdout, "build/main.o: src/main.c\n")
        self.assertIn("include <dir.h> not found", result.stderr)

    def test_system_dependencies_are_omitted(self):
        self.write("sys/sys.h")
        self.write("src/main.c", "#include <sys.h>\n")

        result = self.ok(
            "-S=sys", "--map=src/%.c=build/%.o", "src/main.c"
        )
        self.assertEqual(result.stdout, "build/main.o: src/main.c\n")
        self.assertEqual(result.stderr, "")

    def test_missing_quoted_include_is_fatal(self):
        self.write("src/main.c", "#include \"missing.h\"\n")

        result = self.expect_failure(
            "--map=src/%.c=build/%.o", "src/main.c"
        )
        self.assertIn('include "missing.h" not found', result.stderr)

    def test_missing_angle_include_warns_and_quiet_suppresses(self):
        self.write("src/main.c", "#include <missing.h>\n")
        common = ("--map=src/%.c=build/%.o", "src/main.c")

        result = self.ok(*common)
        self.assertEqual(result.stdout, "build/main.o: src/main.c\n")
        self.assertIn("include <missing.h> not found", result.stderr)

        quiet = self.ok("-q", *common)
        self.assertEqual(quiet.stdout, "build/main.o: src/main.c\n")
        self.assertEqual(quiet.stderr, "")

    def test_missing_project_directory_is_fatal(self):
        self.write("src/main.c")

        result = self.expect_failure(
            "-I=missing", "--map=src/%.c=build/%.o", "src/main.c"
        )
        self.assertIn("include directory 'missing'", result.stderr)

    def test_missing_system_directory_warns_and_quiet_suppresses(self):
        self.write("src/main.c")
        common = ("-S=missing", "--map=src/%.c=build/%.o", "src/main.c")

        result = self.ok(*common)
        self.assertEqual(result.stdout, "build/main.o: src/main.c\n")
        self.assertIn("system include directory 'missing'", result.stderr)

        quiet = self.ok("-q", *common)
        self.assertEqual(quiet.stdout, "build/main.o: src/main.c\n")
        self.assertEqual(quiet.stderr, "")


class MappingTests(DepiTestCase):
    def setUp(self):
        super().setUp()
        self.write("src/main.c")

    def test_mapping_is_required(self):
        result = self.expect_failure("src/main.c")
        self.assertIn("no object mapping", result.stderr)

    def test_exact_mapping(self):
        result = self.ok(
            "--map=src/main.c=build/special.o", "src/main.c"
        )
        self.assertEqual(
            result.stdout, "build/special.o: src/main.c\n"
        )

    def test_wildcard_mapping_matches_slashes(self):
        self.write("src/sub/main.c")
        result = self.ok(
            "--map=src/%.c=build/%.o", "src/sub/main.c"
        )
        self.assertEqual(
            result.stdout, "build/sub/main.o: src/sub/main.c\n"
        )

    def test_first_matching_map_wins(self):
        result = self.ok(
            "--map=src/%.c=first/%.o",
            "--map=%.c=second/%.o",
            "src/main.c",
        )
        self.assertEqual(
            result.stdout, "first/main.o: src/main.c\n"
        )

    def test_replacement_without_percent_is_literal(self):
        result = self.ok(
            "--map=src/%.c=fixed.o", "src/main.c"
        )
        self.assertEqual(result.stdout, "fixed.o: src/main.c\n")

    def test_no_matching_map_is_fatal(self):
        result = self.expect_failure(
            "--map=other/%.c=build/%.o", "src/main.c"
        )
        self.assertIn("no object mapping", result.stderr)

    def test_malformed_maps(self):
        for spec in ("", "=x", "src/%.c=", "a%b%c=x"):
            with self.subTest(spec=spec):
                result = self.run_depi(f"--map={spec}", "src/main.c")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotEqual(result.stderr, "")


class GraphTests(DepiTestCase):
    def test_transitive_closure(self):
        self.write("src/main.c", "#include \"a.h\"\n#include <b.h>\n")
        self.write("src/a.h", "#include <b.h>\n")
        self.write("inc/b.h", "#include <c.h>\n")
        self.write("inc/c.h")

        result = self.ok(
            "-I=inc", "--map=src/%.c=build/%.o", "src/main.c"
        )
        self.assertEqual(
            result.stdout,
            lines(
                "build/main.o: src/main.c src/a.h inc/b.h inc/c.h",
                "src/a.h:",
                "inc/b.h:",
                "inc/c.h:",
            ),
        )

    def test_cycles_terminate(self):
        self.write("src/main.c", "#include <a.h>\n")
        self.write("inc/a.h", "#include <b.h>\n")
        self.write("inc/b.h", "#include <a.h>\n")

        result = self.ok(
            "-I=inc", "--map=src/%.c=build/%.o", "src/main.c"
        )
        self.assertEqual(
            result.stdout,
            lines(
                "build/main.o: src/main.c inc/a.h inc/b.h",
                "inc/a.h:",
                "inc/b.h:",
            ),
        )

    def test_multiple_roots_share_stubs(self):
        self.write("src/a.c", "#include <x.h>\n")
        self.write("src/b.c", "#include <x.h>\n")
        self.write("inc/x.h")

        result = self.ok(
            "-I=inc",
            "--map=src/%.c=build/%.o",
            "src/a.c",
            "src/b.c",
        )
        self.assertEqual(
            result.stdout,
            lines(
                "build/a.o: src/a.c inc/x.h",
                "build/b.o: src/b.c inc/x.h",
                "inc/x.h:",
            ),
        )

    def test_root_without_dependencies(self):
        self.write("src/empty.c")
        result = self.ok(
            "--map=src/%.c=build/%.o", "src/empty.c"
        )
        self.assertEqual(
            result.stdout, "build/empty.o: src/empty.c\n"
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_root_aliases_are_deduplicated(self):
        self.write("src/main.c")
        alias = self.dir / "src" / "alias.c"
        try:
            os.symlink("main.c", alias)
        except OSError as exc:
            self.skipTest(f"cannot create symlink: {exc}")

        result = self.ok(
            "--map=src/%.c=build/%.o",
            "src/main.c",
            "src/alias.c",
        )
        self.assertEqual(
            result.stdout, "build/main.o: src/main.c\n"
        )


class StdinTests(DepiTestCase):
    def test_stdin_list(self):
        self.write("src/a.c")
        self.write("src/b.c")
        result = self.ok(
            "--map=src/%.c=build/%.o",
            "-",
            input="src/a.c\nsrc/b.c\n",
        )
        self.assertEqual(
            result.stdout,
            lines(
                "build/a.o: src/a.c",
                "build/b.o: src/b.c",
            ),
        )

    def test_blank_and_duplicate_lines(self):
        self.write("src/a.c")
        result = self.ok(
            "--map=src/%.c=build/%.o",
            "-",
            input="\nsrc/a.c\n\nsrc/a.c\n",
        )
        self.assertEqual(
            result.stdout, "build/a.o: src/a.c\n"
        )

    def test_mixed_cli_and_stdin(self):
        self.write("src/a.c")
        self.write("src/b.c")
        result = self.ok(
            "--map=src/%.c=build/%.o",
            "src/a.c",
            "-",
            input="src/b.c\n",
        )
        self.assertEqual(
            result.stdout,
            lines(
                "build/a.o: src/a.c",
                "build/b.o: src/b.c",
            ),
        )

    def test_multiple_dashes_read_stdin_once(self):
        self.write("src/a.c")
        result = self.ok(
            "--map=src/%.c=build/%.o",
            "-",
            "-",
            input="src/a.c\n",
        )
        self.assertEqual(
            result.stdout, "build/a.o: src/a.c\n"
        )

    def test_path_with_spaces(self):
        self.write("src/with space.c")
        result = self.ok(
            "--map=src/with space.c=build/with space.o",
            "-",
            input="src/with space.c\n",
        )
        self.assertEqual(
            result.stdout,
            "build/with\\ space.o: src/with\\ space.c\n",
        )

    def test_empty_stdin(self):
        result = self.expect_failure(
            "--map=src/%.c=build/%.o",
            "-",
            input="",
        )
        self.assertIn("no input files", result.stderr)

    def test_literal_dash_file(self):
        self.write("-")
        result = self.ok(
            "--map=./-=build/dash.o", "./-"
        )
        self.assertEqual(
            result.stdout, "build/dash.o: ./-\n"
        )


class DepfileTests(DepiTestCase):
    def setUp(self):
        super().setUp()
        self.write("src/main.c", "#include \"a.h\"\n#include <b.h>\n")
        self.write("src/a.h")
        self.write("inc/b.h")
        self.write("sys/sys.h")

    def test_self_dependency_omits_system_headers(self):
        self.write(
            "src/main.c",
            "#include \"a.h\"\n#include <b.h>\n#include <sys.h>\n",
        )
        result = self.ok(
            "-I=inc",
            "-S=sys",
            "--map=src/%.c=build/%.o",
            "--depfile=.depi.mk",
            "src/main.c",
        )
        self.assertEqual(
            result.stdout,
            lines(
                "build/main.o: src/main.c src/a.h inc/b.h",
                "src/a.h:",
                "inc/b.h:",
                ".depi.mk: src/main.c src/a.h inc/b.h",
            ),
        )
        self.assertNotIn("sys.h", result.stdout)

    def test_no_self_dependency_without_option(self):
        result = self.ok(
            "-I=inc", "--map=src/%.c=build/%.o", "src/main.c"
        )
        self.assertNotIn(".depi.mk:", result.stdout)

    def test_depfile_path_is_make_escaped(self):
        self.write("src/empty.c")
        result = self.ok(
            "--map=src/%.c=build/%.o",
            "--depfile=dep file.mk",
            "src/empty.c",
        )
        self.assertEqual(
            result.stdout,
            lines(
                "build/empty.o: src/empty.c",
                "dep\\ file.mk: src/empty.c",
            ),
        )

    def test_empty_depfile_is_rejected(self):
        result = self.expect_failure(
            "--map=src/%.c=build/%.o",
            "--depfile=",
            "src/main.c",
        )
        self.assertIn("empty depfile path", result.stderr)

    def test_duplicate_depfile_is_rejected(self):
        result = self.expect_failure(
            "--map=src/%.c=build/%.o",
            "--depfile=a",
            "--depfile=b",
            "src/main.c",
        )
        self.assertIn("--depfile specified more than once", result.stderr)


class EscapingTests(DepiTestCase):
    def test_make_special_characters_are_escaped(self):
        source = "src/a #$ b.c"
        obj = "build/a #$ b.o"
        self.write(source)
        result = self.ok(
            f"--map={source}={obj}", source
        )
        self.assertEqual(
            result.stdout,
            "build/a\\ \\#$$\\ b.o: src/a\\ \\#$$\\ b.c\n",
        )

    def test_backslashes_are_escaped(self):
        source = "src/back\\slash.c"
        obj = "build/back\\slash.o"
        self.write(source)
        result = self.ok(
            f"--map={source}={obj}", source
        )
        self.assertEqual(
            result.stdout,
            "build/back\\\\slash.o: src/back\\\\slash.c\n",
        )


@unittest.skipUnless(MAKE, "GNU make (gmake or make) is required")
class MakeIntegrationTests(DepiTestCase):
    def write_makefile(self, srcs):
        makefile = f"""\
.POSIX:
DEPI = depi
DEPFILE = .depi.mk
SRCS = {srcs}

all: src/main.o

.c.o:
\t@echo BUILD $@
\t@touch $@

$(DEPFILE): $(SRCS) $(DEPI)
\t@tmp="$@.$$$$"; \\
\ttrap 'rm -f "$$tmp"' 0; \\
\ttrap 'exit 1' 1 2 15; \\
\tprintf '%s\\n' $(SRCS) | \\
\t    "$(DEPI)" --depfile="$@" --map='%.c=%.o' - > "$$tmp" && \\
\tmv "$$tmp" "$@"

include $(DEPFILE)
"""
        (self.dir / "Makefile").write_text(makefile)

    def run_make(self, *args, depi=DEPI, timeout=15):
        return subprocess.run(
            [MAKE, "-f", "Makefile", f"DEPI={depi}", *args],
            cwd=self.dir,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def test_build_depfile_and_conditional_rebuild(self):
        self.write("src/main.c", "#include \"a.h\"\n")
        self.write("src/a.h")
        self.write_makefile("src/main.c")

        first = self.run_make()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("BUILD src/main.o", first.stdout)
        depfile = (self.dir / ".depi.mk").read_text()
        self.assertIn("src/main.o: src/main.c src/a.h", depfile)
        self.assertIn(".depi.mk: src/main.c src/a.h", depfile)
        self.assertEqual(list(self.dir.glob(".depi.mk.*")), [])

        second = self.run_make()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("BUILD", second.stdout)

        time.sleep(0.02)
        self.write("src/b.h")
        self.write("src/a.h", "#include \"b.h\"\n")
        third = self.run_make()
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertIn("BUILD src/main.o", third.stdout)
        depfile = (self.dir / ".depi.mk").read_text()
        self.assertIn("src/main.o: src/main.c src/a.h src/b.h", depfile)

    def test_documented_pattern_terminates(self):
        self.write("src/main.c", "#include \"a.h\"\n")
        self.write("src/a.h")
        self.write_makefile("src/main.c")

        try:
            result = self.run_make("-n", timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("make -n did not terminate")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("src/main.o", result.stdout)

    def test_failed_generation_cleans_temporary_file(self):
        self.write("src/main.c")
        self.write("src/bad.c", "#include \"missing.h\"\n")
        self.write_makefile("src/main.c src/bad.c")

        result = self.run_make()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.dir.glob(".depi.mk.*")), [])
        self.assertFalse((self.dir / ".depi.mk").exists())
        self.assertIn("missing.h", result.stderr)

    def test_cancelled_generation_cleans_temporary_file(self):
        self.write("src/main.c", "#include \"a.h\"\n")
        self.write("src/a.h")
        self.write_makefile("src/main.c")

        fake_depi = self.dir / "fake-depi"
        fake_depi.write_text(
            "#!/bin/sh\n"
            "sleep 30\n"
            f"exec {shlex.quote(DEPI)} \"$@\"\n"
        )
        fake_depi.chmod(0o755)
        (self.dir / ".depi.mk").unlink(missing_ok=True)

        proc = subprocess.Popen(
            [MAKE, "-f", "Makefile", f"DEPI={fake_depi}"],
            cwd=self.dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            seen = False
            while time.monotonic() < deadline:
                if list(self.dir.glob(".depi.mk.*")):
                    seen = True
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.05)

            self.assertTrue(seen, "temporary depfile was not created")
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)

        self.assertEqual(list(self.dir.glob(".depi.mk.*")), [])


if __name__ == "__main__":
    unittest.main()
