from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


SESSION_DAYS = 14
WORKSPACE_ROLES = {"viewer": 1, "editor": 2, "admin": 3, "owner": 4}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"


def _password_valid(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(candidate, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


class PlatformDB:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.reports_dir = data_dir / "reports"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "pbi-runner.sqlite3"
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    global_role TEXT NOT NULL DEFAULT 'user' CHECK(global_role IN ('admin','user')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_by INTEGER NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_members (
                    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('owner','admin','editor','viewer')),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_type TEXT NOT NULL CHECK(source_type IN ('PBIX','PBIP')),
                    source_path TEXT NOT NULL,
                    uploaded_by INTEGER NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_reports_workspace ON reports(workspace_id);
                """
            )
            workspace_columns = {row[1] for row in db.execute("PRAGMA table_info(workspaces)")}
            if "logo" not in workspace_columns:
                db.execute("ALTER TABLE workspaces ADD COLUMN logo BLOB")
            if "logo_mime" not in workspace_columns:
                db.execute("ALTER TABLE workspaces ADD COLUMN logo_mime TEXT")

    def configured(self) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    @staticmethod
    def public_user(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "email": row["email"], "globalRole": row["global_role"]}

    def _new_session(self, db: sqlite3.Connection, user_id: int) -> str:
        token = secrets.token_urlsafe(40)
        expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
        db.execute(
            "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), user_id, expires, _now()),
        )
        return token

    def setup(self, name: str, email: str, password: str) -> tuple[dict[str, Any], str]:
        name, email = name.strip(), email.strip().lower()
        self._validate_user(name, email, password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise ValueError("A configuração inicial já foi concluída")
            cursor = db.execute(
                "INSERT INTO users(name,email,password_hash,global_role,created_at) VALUES(?,?,?,?,?)",
                (name, email, _password_hash(password), "admin", _now()),
            )
            user_id = cursor.lastrowid
            workspace = db.execute(
                "INSERT INTO workspaces(name,description,created_by,created_at) VALUES(?,?,?,?)",
                ("Meu workspace", "Workspace inicial", user_id, _now()),
            ).lastrowid
            db.execute(
                "INSERT INTO workspace_members(workspace_id,user_id,role,created_at) VALUES(?,?,?,?)",
                (workspace, user_id, "owner", _now()),
            )
            token = self._new_session(db, user_id)
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return self.public_user(row), token

    @staticmethod
    def _validate_user(name: str, email: str, password: str) -> None:
        if len(name) < 2:
            raise ValueError("Informe um nome com pelo menos 2 caracteres")
        if "@" not in email or len(email) > 254:
            raise ValueError("Informe um e-mail válido")
        if len(password) < 8:
            raise ValueError("A senha precisa ter pelo menos 8 caracteres")

    def login(self, email: str, password: str) -> tuple[dict[str, Any], str]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE email=? AND active=1", (email.strip().lower(),)).fetchone()
            if not row or not _password_valid(password, row["password_hash"]):
                raise PermissionError("E-mail ou senha inválidos")
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
            return self.public_user(row), self._new_session(db, row["id"])

    def user_for_token(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
                (digest, _now()),
            ).fetchone()
            return self.public_user(row) if row else None

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def create_user(self, actor: dict[str, Any], name: str, email: str, password: str, global_role: str = "user") -> dict[str, Any]:
        if actor["globalRole"] != "admin":
            raise PermissionError("Apenas administradores podem cadastrar usuários")
        if global_role not in {"admin", "user"}:
            raise ValueError("Papel global inválido")
        name, email = name.strip(), email.strip().lower()
        self._validate_user(name, email, password)
        with self.connect() as db:
            try:
                user_id = db.execute(
                    "INSERT INTO users(name,email,password_hash,global_role,created_at) VALUES(?,?,?,?,?)",
                    (name, email, _password_hash(password), global_role, _now()),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ValueError("Já existe um usuário com este e-mail") from exc
            return self.public_user(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def list_users(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        if actor["globalRole"] != "admin":
            raise PermissionError("Apenas administradores podem listar usuários")
        with self.connect() as db:
            return [self.public_user(row) for row in db.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE")]

    def create_workspace(self, actor: dict[str, Any], name: str, description: str = "") -> dict[str, Any]:
        name = name.strip()
        if len(name) < 2:
            raise ValueError("Informe um nome para o workspace")
        with self.connect() as db:
            workspace_id = db.execute(
                "INSERT INTO workspaces(name,description,created_by,created_at) VALUES(?,?,?,?)",
                (name, description.strip(), actor["id"], _now()),
            ).lastrowid
            db.execute(
                "INSERT INTO workspace_members(workspace_id,user_id,role,created_at) VALUES(?,?,?,?)",
                (workspace_id, actor["id"], "owner", _now()),
            )
        return self.get_workspace(actor, workspace_id)

    def workspace_role(self, actor: dict[str, Any], workspace_id: int) -> str | None:
        if actor["globalRole"] == "admin":
            return "owner"
        with self.connect() as db:
            row = db.execute(
                "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
                (workspace_id, actor["id"]),
            ).fetchone()
            return row["role"] if row else None

    def require_workspace(self, actor: dict[str, Any], workspace_id: int, minimum: str = "viewer") -> str:
        role = self.workspace_role(actor, workspace_id)
        if not role or WORKSPACE_ROLES[role] < WORKSPACE_ROLES[minimum]:
            raise PermissionError("Você não possui permissão neste workspace")
        return role

    def list_workspaces(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        with self.connect() as db:
            if actor["globalRole"] == "admin":
                rows = db.execute(
                    "SELECT w.*,COALESCE(m.role,'owner') role,COUNT(DISTINCT r.id) report_count "
                    "FROM workspaces w LEFT JOIN workspace_members m ON m.workspace_id=w.id AND m.user_id=? "
                    "LEFT JOIN reports r ON r.workspace_id=w.id GROUP BY w.id ORDER BY w.name COLLATE NOCASE",
                    (actor["id"],),
                )
            else:
                rows = db.execute(
                    "SELECT w.*,m.role,COUNT(DISTINCT r.id) report_count FROM workspaces w "
                    "JOIN workspace_members m ON m.workspace_id=w.id AND m.user_id=? "
                    "LEFT JOIN reports r ON r.workspace_id=w.id GROUP BY w.id ORDER BY w.name COLLATE NOCASE",
                    (actor["id"],),
                )
            return [self._workspace(row) for row in rows]

    @staticmethod
    def _workspace(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "description": row["description"], "role": row["role"], "reportCount": row["report_count"], "hasLogo": bool(row["logo"])}

    def get_workspace(self, actor: dict[str, Any], workspace_id: int) -> dict[str, Any]:
        role = self.require_workspace(actor, workspace_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
            if not row:
                raise FileNotFoundError("Workspace não encontrado")
            reports = [dict(item) for item in db.execute(
                "SELECT id,name,source_name sourceName,source_type sourceType,created_at createdAt,updated_at updatedAt "
                "FROM reports WHERE workspace_id=? ORDER BY updated_at DESC", (workspace_id,)
            )]
            members = []
            if WORKSPACE_ROLES[role] >= WORKSPACE_ROLES["admin"]:
                members = [dict(item) for item in db.execute(
                    "SELECT u.id,u.name,u.email,m.role FROM workspace_members m JOIN users u ON u.id=m.user_id "
                    "WHERE m.workspace_id=? ORDER BY u.name COLLATE NOCASE", (workspace_id,)
                )]
            return {"id": row["id"], "name": row["name"], "description": row["description"], "role": role, "hasLogo": bool(row["logo"]), "reports": reports, "members": members}

    def set_workspace_logo(self, actor: dict[str, Any], workspace_id: int, mime: str, data: bytes) -> None:
        self.require_workspace(actor, workspace_id, "admin")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
                raise FileNotFoundError("Workspace não encontrado")
            db.execute("UPDATE workspaces SET logo=?,logo_mime=? WHERE id=?", (data, mime, workspace_id))

    def get_workspace_logo(self, actor: dict[str, Any], workspace_id: int) -> tuple[str, bytes]:
        self.require_workspace(actor, workspace_id)
        with self.connect() as db:
            row = db.execute("SELECT logo,logo_mime FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
            if not row or not row["logo"]:
                raise FileNotFoundError("Logo não encontrada")
            return row["logo_mime"], bytes(row["logo"])

    def set_member(self, actor: dict[str, Any], workspace_id: int, email: str, role: str) -> dict[str, Any]:
        actor_role = self.require_workspace(actor, workspace_id, "admin")
        if role not in WORKSPACE_ROLES:
            raise ValueError("Papel de workspace inválido")
        if role == "owner" and actor_role != "owner":
            raise PermissionError("Apenas um proprietário pode atribuir o papel owner")
        with self.connect() as db:
            user = db.execute("SELECT * FROM users WHERE email=? AND active=1", (email.strip().lower(),)).fetchone()
            if not user:
                raise FileNotFoundError("Usuário não encontrado; cadastre-o primeiro")
            db.execute(
                "INSERT INTO workspace_members(workspace_id,user_id,role,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(workspace_id,user_id) DO UPDATE SET role=excluded.role",
                (workspace_id, user["id"], role, _now()),
            )
            return {**self.public_user(user), "role": role}

    def add_report(self, actor: dict[str, Any], workspace_id: int, name: str, source_name: str, source_type: str, source_path: Path) -> dict[str, Any]:
        self.require_workspace(actor, workspace_id, "editor")
        with self.connect() as db:
            report_id = db.execute(
                "INSERT INTO reports(workspace_id,name,source_name,source_type,source_path,uploaded_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (workspace_id, name, source_name, source_type, str(source_path), actor["id"], _now(), _now()),
            ).lastrowid
            return dict(db.execute(
                "SELECT id,name,source_name sourceName,source_type sourceType,created_at createdAt,updated_at updatedAt FROM reports WHERE id=?",
                (report_id,),
            ).fetchone())

    def get_report(self, actor: dict[str, Any], report_id: int, minimum: str = "viewer") -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            if not row:
                raise FileNotFoundError("Relatório não encontrado")
            self.require_workspace(actor, row["workspace_id"], minimum)
            return dict(row)

    def delete_report(self, actor: dict[str, Any], report_id: int) -> dict[str, Any]:
        """Remove a report metadata row after enforcing workspace access."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            if not row:
                raise FileNotFoundError("Relatório não encontrado")
            self.require_workspace(actor, row["workspace_id"], "editor")
            db.execute("DELETE FROM reports WHERE id=?", (report_id,))
            return dict(row)
