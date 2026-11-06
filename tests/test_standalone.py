import tempfile
import unittest
from pathlib import Path

import httpx
import auto_server
import store


class StandaloneServiceTest(unittest.TestCase):
    def test_local_service_opens_requested_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo.db"
            db = store.open(str(path))
            db.set("resume_point", "example-commit")
            db.hset("ingest_run", mapping={"status": "done"})
            db.close()
            try:
                url = auto_server.service_url(path)
                response = httpx.get(f"{url}/api/repository", timeout=5)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["indexed_commit"], "example-commit")
                self.assertEqual(httpx.get(f"{url}/health", timeout=5).json()["db"], str(path))
            finally:
                auto_server._stop_services()
                for process, _ in auto_server._services.values():
                    process.wait(timeout=10)
                auto_server._services.clear()


if __name__ == "__main__":
    unittest.main()
