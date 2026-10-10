"""CI tier config: every listed module exists, sits in one tier, and core covers the others."""
import unittest

from run_shard import CORE, all_modules, load_shards, modules_for


class ShardConfigTests(unittest.TestCase):
    def test_listed_modules_exist_once_and_tiers_partition_the_suite(self):
        shards, modules = load_shards(), all_modules()
        listed = [module for names in shards.values() for module in names]
        self.assertEqual(sorted(set(listed) - modules), [])
        self.assertEqual(len(listed), len(set(listed)))
        selections = {name: modules_for(name, shards, modules) for name in [*shards, CORE]}
        self.assertEqual(set().union(*selections.values()), modules)
        self.assertEqual(sum(len(selected) for selected in selections.values()), len(modules))
        self.assertIn("test_ci_shards", selections[CORE])

    def test_unknown_names_fail_loudly(self):
        modules = all_modules()
        with self.assertRaises(SystemExit):
            modules_for(CORE, {"study": ["test_no_such_module"]}, modules)
        with self.assertRaises(SystemExit):
            modules_for("study-x", {}, modules)


if __name__ == "__main__":
    unittest.main()
