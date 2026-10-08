"""CI sharding config: every listed module exists, sits in one shard, and rest covers the others."""
import unittest

from run_shard import REST, all_modules, load_shards, modules_for


class ShardConfigTests(unittest.TestCase):
    def test_listed_modules_exist_once_and_shards_partition_the_suite(self):
        shards, modules = load_shards(), all_modules()
        listed = [module for names in shards.values() for module in names]
        self.assertEqual(sorted(set(listed) - modules), [])
        self.assertEqual(len(listed), len(set(listed)))
        selections = {name: modules_for(name, shards, modules) for name in [*shards, REST]}
        self.assertEqual(set().union(*selections.values()), modules)
        self.assertEqual(sum(len(selected) for selected in selections.values()), len(modules))
        self.assertIn("test_ci_shards", selections[REST])

    def test_unknown_names_fail_loudly(self):
        modules = all_modules()
        with self.assertRaises(SystemExit):
            modules_for(REST, {"heavy-x": ["test_no_such_module"]}, modules)
        with self.assertRaises(SystemExit):
            modules_for("heavy-x", {}, modules)


if __name__ == "__main__":
    unittest.main()
