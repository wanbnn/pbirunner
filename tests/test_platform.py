import tempfile
import unittest
import zipfile
from pathlib import Path

from pbi_viewer.platform import PlatformDB
from pbi_viewer.server import ApplicationState


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.db = PlatformDB(self.data_dir)

    def tearDown(self):
        self.temporary.cleanup()

    def test_first_run_creates_admin_workspace_and_session(self):
        self.assertFalse(self.db.configured())
        admin, token = self.db.setup("Administrador", "admin@example.com", "segredo123")
        self.assertEqual(admin["globalRole"], "admin")
        self.assertEqual(self.db.user_for_token(token)["email"], "admin@example.com")
        workspaces = self.db.list_workspaces(admin)
        self.assertEqual(len(workspaces), 1)
        self.assertEqual(workspaces[0]["role"], "owner")
        with self.assertRaises(ValueError):
            self.db.setup("Outro", "outro@example.com", "segredo123")

    def test_workspace_roles_limit_changes_but_allow_viewing(self):
        admin, _ = self.db.setup("Administrador", "admin@example.com", "segredo123")
        viewer = self.db.create_user(admin, "Leitor", "leitor@example.com", "segredo123")
        workspace = self.db.list_workspaces(admin)[0]
        self.db.set_member(admin, workspace["id"], viewer["email"], "viewer")
        self.assertEqual(self.db.require_workspace(viewer, workspace["id"]), "viewer")
        with self.assertRaises(PermissionError):
            self.db.require_workspace(viewer, workspace["id"], "editor")

    def test_password_authentication_and_logout(self):
        self.db.setup("Administrador", "admin@example.com", "segredo123")
        with self.assertRaises(PermissionError):
            self.db.login("admin@example.com", "incorreta")
        user, token = self.db.login("ADMIN@example.com", "segredo123")
        self.assertEqual(user["email"], "admin@example.com")
        self.db.logout(token)
        self.assertIsNone(self.db.user_for_token(token))

    def test_zip_path_traversal_is_rejected(self):
        archive = self.data_dir / "unsafe.zip"
        destination = self.data_dir / "extract"
        destination.mkdir()
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("../escape.pbip", "{}")
        with self.assertRaises(ValueError):
            ApplicationState._extract_zip(archive, destination)

    def test_report_deletion_requires_editor_and_removes_metadata(self):
        admin, _ = self.db.setup("Administrador", "admin@example.com", "segredo123")
        viewer = self.db.create_user(admin, "Leitor", "leitor@example.com", "segredo123")
        workspace = self.db.list_workspaces(admin)[0]
        self.db.set_member(admin, workspace["id"], viewer["email"], "viewer")
        source = self.data_dir / "report.pbix"
        source.write_bytes(b"pbix")
        report = self.db.add_report(admin, workspace["id"], "Relatório", "report.pbix", "PBIX", source)
        with self.assertRaises(PermissionError):
            self.db.delete_report(viewer, report["id"])
        removed = self.db.delete_report(admin, report["id"])
        self.assertEqual(removed["id"], report["id"])
        with self.assertRaises(FileNotFoundError):
            self.db.get_report(admin, report["id"])

    def test_workspace_logo_is_persistent_and_requires_admin(self):
        admin, _ = self.db.setup("Administrador", "admin@example.com", "segredo123")
        editor = self.db.create_user(admin, "Editor", "editor@example.com", "segredo123")
        workspace = self.db.list_workspaces(admin)[0]
        self.db.set_member(admin, workspace["id"], editor["email"], "editor")
        with self.assertRaises(PermissionError):
            self.db.set_workspace_logo(editor, workspace["id"], "image/png", b"png")
        self.db.set_workspace_logo(admin, workspace["id"], "image/svg+xml", b"<svg/>")
        mime, data = self.db.get_workspace_logo(editor, workspace["id"])
        self.assertEqual((mime, data), ("image/svg+xml", b"<svg/>"))
        self.assertTrue(self.db.get_workspace(admin, workspace["id"])["hasLogo"])

    def test_owner_deletes_workspace_and_all_report_files(self):
        owner, _ = self.db.setup("Proprietário", "owner@example.com", "segredo123")
        editor = self.db.create_user(owner, "Editor", "editor@example.com", "segredo123")
        workspace = self.db.list_workspaces(owner)[0]
        self.db.set_member(owner, workspace["id"], editor["email"], "editor")
        report_dir = self.db.reports_dir / "workspace-delete-test"
        report_dir.mkdir()
        source = report_dir / "report.pbix"
        source.write_bytes(b"pbix")
        report = self.db.add_report(owner, workspace["id"], "Relatório", source.name, "PBIX", source)
        with self.assertRaises(PermissionError):
            self.db.delete_workspace(editor, workspace["id"])
        state = ApplicationState(self.data_dir)
        state.delete_workspace(owner, workspace["id"])
        state.close()
        self.assertFalse(report_dir.exists())
        with self.assertRaises(FileNotFoundError):
            self.db.get_report(owner, report["id"])
        self.assertEqual(self.db.list_workspaces(owner), [])


if __name__ == "__main__":
    unittest.main()
