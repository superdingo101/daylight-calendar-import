"""Export mutmut 3.8.0 survivor diagnostics without spawning a CLI per mutant.

The CI reports deliberately keep the existing artifact names and `mutmut show`
format so an unsuccessful mutation gate remains easy to investigate.
"""

from __future__ import annotations

import sys
from pathlib import Path

from mutmut.mutation.data import SourceFileMutationData
from mutmut.mutation.diff_apply import get_diff_for_mutant
from mutmut.stats import status_by_exit_code
from mutmut.utils.file_utils import walk_mutatable_files


RESULTS_PATH = Path("mutation-survivors.txt")
NAMES_PATH = Path("mutation-survivor-names.txt")
REPORT_PATH = Path("mutation-survivor-report.txt")


def main() -> int:
    """Generate the same diagnostics as `mutmut results` and `mutmut show`."""
    found_mutants = False
    survivor_count = 0
    export_failures = 0

    with (
        RESULTS_PATH.open("w", encoding="utf-8") as results,
        NAMES_PATH.open("w", encoding="utf-8") as names,
        REPORT_PATH.open("w", encoding="utf-8") as report,
    ):
        for path in walk_mutatable_files():
            metadata = SourceFileMutationData(path=path)
            metadata.load()
            if not metadata.exit_code_by_key:
                continue
            found_mutants = True

            for mutant, exit_code in metadata.exit_code_by_key.items():
                status = status_by_exit_code[exit_code]
                if status == "killed":
                    continue

                results.write(f"    {mutant}: {status}\n")
                if status != "survived":
                    continue

                survivor_count += 1
                names.write(f"{mutant}\n")
                try:
                    diff = get_diff_for_mutant(mutant, path=metadata.path)
                except Exception as exc:
                    export_failures += 1
                    print(f"ERROR: cannot export {mutant}: {exc}", file=sys.stderr)
                    report.write(f"===== {mutant} =====\nERROR: diff export failed\n\n")
                    continue

                report.write(f"===== {mutant} =====\n# {mutant}: survived\n{diff}\n\n")

    if not found_mutants:
        print("ERROR: no mutation results found; was `mutmut run` completed?", file=sys.stderr)
        return 1
    if export_failures:
        print(f"ERROR: failed to export {export_failures} survivor diffs", file=sys.stderr)
        return 1

    print(f"Exported {survivor_count} surviving mutants and their diffs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
