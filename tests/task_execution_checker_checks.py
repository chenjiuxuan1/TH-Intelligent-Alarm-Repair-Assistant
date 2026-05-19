import importlib.util
import os
import unittest
from unittest import mock


MODULE_PATH = "/Users/jiangchuanchen/Desktop/TH-Intelligent-Alarm-Repair-Assistant/tools/task_execution_checker.py"


def load_module():
    spec = importlib.util.spec_from_file_location("task_execution_checker", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TaskExecutionCheckerTests(unittest.TestCase):
    def test_checker_falls_back_to_repo_root_when_configured_workspace_is_invalid(self):
        existing_paths = {
            "/Users/jiangchuanchen/Desktop/TH-Intelligent-Alarm-Repair-Assistant/core/repair_strict_7step.py"
        }

        with mock.patch.dict(os.environ, {"APP_WORKSPACE": "/invalid/workspace"}, clear=False), mock.patch(
            "os.path.exists", side_effect=lambda path: path in existing_paths
        ):
            module = load_module()

        self.assertEqual(
            module.EFFECTIVE_WORKSPACE_ROOT,
            "/Users/jiangchuanchen/Desktop/TH-Intelligent-Alarm-Repair-Assistant",
        )
        self.assertTrue(module.check_script_exists("core/repair_strict_7step.py"))

    def test_env_check_accepts_runtime_environment_over_placeholders(self):
        with mock.patch.dict(
            os.environ,
            {"DS_TOKEN": "token-from-runtime", "DB_PASSWORD": "db-pass-from-runtime"},
            clear=False,
        ):
            module = load_module()
            module.DS_CONFIG["token"] = "REPLACE_WITH_TH_DS_TOKEN"
            module.DB_CONFIG["password"] = "REPLACE_WITH_TH_DB_PASSWORD"

            self.assertEqual(module.check_env_variables(), [])

    def test_env_check_treats_placeholders_as_missing(self):
        module = load_module()
        module.DS_CONFIG["token"] = "REPLACE_WITH_TH_DS_TOKEN"
        module.DB_CONFIG["password"] = "REPLACE_WITH_TH_DB_PASSWORD"

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(module.check_env_variables(), ["DS_TOKEN", "DB_PASSWORD"])

    def test_env_check_accepts_real_values_from_loaded_config(self):
        module = load_module()
        module.DS_CONFIG["token"] = "token-from-local-config"
        module.DB_CONFIG["password"] = "db-pass-from-local-config"

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(module.check_env_variables(), [])


if __name__ == "__main__":
    unittest.main()
