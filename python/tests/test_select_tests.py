"""Study-test selection from the import graph (tiny temporary package, no real modules)."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from run_shard import load_shards
from select_tests import select

STUDY = ["test_study_a", "test_study_b", "test_study_c"]


def write(root: Path, relative: str, text: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SelectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        py = self.root / "python"
        write(py, "pkg/__init__.py")
        write(py, "pkg/low.py", "VALUE = 1\n")
        write(py, "pkg/mid.py", "from .low import VALUE\n")
        write(py, "pkg/sub/__init__.py")
        write(py, "pkg/sub/leaf.py", "X = 1\n")
        write(py, "pkg/top.py", "from . import mid\nfrom .sub import leaf\n")
        write(py, "pkg/bench.py", "Y = 1\n")
        write(py, "tests/helper.py", "from pkg.low import VALUE\n")
        write(py, "tests/test_study_a.py", "from pkg.top import mid\n")      # a -> top -> mid -> low
        write(py, "tests/test_study_b.py", "from pkg.sub import leaf\nimport helper\n")
        write(py, "tests/test_study_c.py", "from pkg import bench\n")        # `from pkg import submodule`

    def run_select(self, *changed):
        return select(list(changed), STUDY, self.root / "python", self.root, package="pkg")

    def test_transitive_and_relative_imports(self):
        self.assertEqual(self.run_select("python/pkg/low.py"), ["test_study_a", "test_study_b"])  # b via helper
        self.assertEqual(self.run_select("python/pkg/mid.py"), ["test_study_a"])

    def test_from_package_import_submodule_and_package_init(self):
        self.assertEqual(self.run_select("python/pkg/bench.py"), ["test_study_c"])
        self.assertEqual(self.run_select("python/pkg/sub/leaf.py"), ["test_study_a", "test_study_b"])
        self.assertEqual(self.run_select("python/pkg/__init__.py"), STUDY)  # every import runs it

    def test_the_test_file_itself_and_a_helper_select_their_users(self):
        self.assertEqual(self.run_select("python/tests/test_study_c.py"), ["test_study_c"])
        self.assertEqual(self.run_select("python/tests/helper.py"), ["test_study_b"])

    def test_shared_inputs_run_everything(self):
        for path in ("python/requirements.txt", "python/tests/run_shard.py", "python/tests/shards.json",
                     "python/tests/fixtures/x.csv", ".github/workflows/verify.yml"):
            self.assertEqual(self.run_select(path), STUDY, path)

    def test_benchmark_only_or_unrelated_change_selects_nothing(self):
        write(self.root / "python", "pkg/benchmark_only.py", "Z = 1\n")
        self.assertEqual(self.run_select("python/pkg/benchmark_only.py"), [])
        self.assertEqual(self.run_select("src/app.ts", "docs/x.md", "python/tests/test_core_thing.py"), [])


class RealRepositorySelectionTests(unittest.TestCase):
    """The live-code boundary of the study tier, checked on the real import graph."""
    PYTHON_DIR = Path(__file__).resolve().parent.parent

    def run_select(self, *changed):
        return select(list(changed), load_shards()["study"], self.PYTHON_DIR)

    def test_benchmark_forward_and_frontend_changes_select_no_study_module(self):
        self.assertEqual(self.run_select("python/market_analysis/benchmark/scan.py"), [])
        self.assertEqual(self.run_select("python/market_analysis/forward/setups.py"), [])
        self.assertEqual(self.run_select("python/tests/test_forward_engine.py", "src/lib/forward/x.ts"), [])

    def test_study_source_and_shared_live_code_select_study_modules(self):
        self.assertIn("test_historical_market_state_study_models",
                      self.run_select("python/market_analysis/historical_market_state_study_models.py"))
        self.assertTrue(self.run_select("python/market_analysis/canonical_identity.py"))
        self.assertEqual(self.run_select("python/tests/shards.json"), load_shards()["study"])


if __name__ == "__main__":
    unittest.main()
