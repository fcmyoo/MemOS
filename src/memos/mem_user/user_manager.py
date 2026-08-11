"""User management system for MemOS.

This module provides user authentication, authorization, and cube management
functionality using SQLAlchemy and SQLite.
"""

import sqlite3
import uuid

from datetime import datetime
from enum import Enum
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    String,
    Table,
    create_engine,
)
from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

from memos import settings
from memos.log import get_logger


logger = get_logger(__name__)

Base = declarative_base()

# Bumped whenever a column/index is added to an existing table. create_all()
# only creates *missing tables*, so column additions to pre-existing databases
# are handled by UserManager._migrate_schema() under this version stamp.
SCHEMA_VERSION = 2


class UserRole(Enum):
    """User roles enumeration."""

    ROOT = "ROOT"
    ADMIN = "ADMIN"
    USER = "USER"
    GUEST = "GUEST"


# Association table for many-to-many relationship between users and cubes
user_cube_association = Table(
    "user_cube_association",
    Base.metadata,
    Column("user_id", String, ForeignKey("users.user_id"), primary_key=True),
    Column("cube_id", String, ForeignKey("cubes.cube_id"), primary_key=True),
    Column("created_at", DateTime, default=datetime.now),
)


class User(Base):
    """User model for the database."""

    __tablename__ = "users"

    user_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_name = Column(String, unique=True, nullable=False)
    role = Column(SQLEnum(UserRole), default=UserRole.USER, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    # Web console login (Argon2id PHC string). Nullable: historical users and
    # API-key-only accounts keep working without a password; pre-existing
    # databases gain this column through UserManager._migrate_schema().
    password_hash = Column(String, nullable=True)

    # Relationship with cubes
    cubes = relationship("Cube", secondary=user_cube_association, back_populates="users")
    owned_cubes = relationship("Cube", back_populates="owner", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(user_id='{self.user_id}', user_name='{self.user_name}', role='{self.role.value}')>"


class Cube(Base):
    """Cube model for the database."""

    __tablename__ = "cubes"

    cube_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    cube_name = Column(String, nullable=False)
    cube_path = Column(String, nullable=True)  # Local path or remote repo
    owner_id = Column(String, ForeignKey("users.user_id"), nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # Relationships
    owner = relationship("User", back_populates="owned_cubes")
    users = relationship("User", secondary=user_cube_association, back_populates="cubes")

    def __repr__(self):
        return f"<Cube(cube_id='{self.cube_id}', cube_name='{self.cube_name}', owner_id='{self.owner_id}')>"


class UserManager:
    """User management system for MemOS."""

    def __init__(self, db_path: str | None = None, user_id: str = "root"):
        """Initialize the user manager with database connection.

        Args:
            db_path (str, optional): Path to the SQLite database file.
                If None, uses default path in MEMOS_DIR.
            user_id (str, optional): User ID. If None, uses default user ID.
        """
        if db_path is None:
            db_path = str(settings.MEMOS_DIR / "memos_users.db")

        # Ensure the directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self.db_path = db_path
        self.engine = create_engine(f"sqlite:///{db_path}", echo=False)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        # Create tables, then reconcile pre-existing (legacy) databases that
        # were created before newer columns existed.
        Base.metadata.create_all(bind=self.engine)
        self._migrate_schema()

        # Initialize with root user if no users exist
        self._init_root_user(user_id)

        logger.info(f"UserManager initialized with database at {db_path}")

    def _migrate_schema(self) -> None:
        """Idempotently add columns missing from pre-existing databases.

        ``create_all()`` never alters a table that already exists, so a legacy
        ``users`` table (created before the Web console) lacks
        ``password_hash``. Detect missing columns with ``PRAGMA table_info``
        and add them inside a ``BEGIN IMMEDIATE`` transaction, then stamp
        ``PRAGMA user_version``. Re-running is a no-op, which keeps repeated
        boots safe.
        """
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= SCHEMA_VERSION:
                return

            conn.execute("BEGIN IMMEDIATE")
            user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            if "password_hash" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
                logger.info("Migrated users table: added password_hash column")
            conn.commit()
            # Stamp the version outside the transaction: some SQLite builds
            # reject writing user_version mid-transaction.
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except Exception as e:
            conn.rollback()
            logger.error(f"Schema migration failed: {e}")
            raise
        finally:
            conn.close()

    def _get_session(self) -> Session:
        """Get a database session."""
        return self.SessionLocal()

    def _init_root_user(self, user_id: str) -> None:
        """Initialize the root user if no users exist."""
        session = self._get_session()
        try:
            # Check if any users exist
            user_count = session.query(User).count()
            if user_count == 0:
                root_user = User(user_id=user_id, user_name=user_id, role=UserRole.ROOT)
                session.add(root_user)
                session.commit()
                logger.info("Root user created successfully")
            else:
                self.create_user(user_name=user_id, user_id=user_id, role=UserRole.ROOT)
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to create {user_id} user: {e}")
        finally:
            session.close()

    def create_user(
        self, user_name: str, role: UserRole = UserRole.USER, user_id: str | None = None
    ) -> str:
        """Create a new user.

        Args:
            user_name (str): Name of the user.
            role (UserRole): Role of the user.
            user_id (str, optional): Custom user ID. If None, generates UUID.

        Returns:
            str: The created user ID.

        Raises:
            ValueError: If user_name already exists.
        """
        session = self._get_session()
        try:
            # Check if user_name already exists
            existing_user = session.query(User).filter(User.user_name == user_name).first()
            if existing_user:
                logger.info(f"User with name '{user_name}' already exists")
                return existing_user.user_id
            user = User(user_name=user_name, role=role, user_id=user_id or str(uuid.uuid4()))
            session.add(user)
            session.commit()
            logger.info(f"User '{user_name}' created with ID: {user.user_id}")
            return user.user_id
        except IntegrityError:
            session.rollback()
            logger.info(f"failed to create user with name '{user_name}' already exists")
        except Exception as e:
            session.rollback()
            logger.error(f"Error creating user: {e}")
            raise
        finally:
            session.close()

    def get_user(self, user_id: str) -> User | None:
        """Get user by ID.

        Args:
            user_id (str): The user ID.

        Returns:
            User: The user object or None if not found.
        """
        session = self._get_session()
        try:
            return session.query(User).filter(User.user_id == user_id).first()
        finally:
            session.close()

    def get_user_by_name(self, user_name: str) -> User | None:
        """Get user by name.

        Args:
            user_name (str): The user name.

        Returns:
            User: The user object or None if not found.
        """
        session = self._get_session()
        try:
            return session.query(User).filter(User.user_name == user_name).first()
        finally:
            session.close()

    def set_user_password(self, user_id: str, password_hash: str) -> bool:
        """Set (or update) the Argon2id hash for a Web console account.

        Returns True when the user exists and the hash was persisted.
        """
        session = self._get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if user is None:
                return False
            user.password_hash = password_hash
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def validate_user(self, user_id: str) -> bool:
        """Validate if a user exists and is active.

        Args:
            user_id (str): The user ID to validate.

        Returns:
            bool: True if user exists and is active, False otherwise.
        """
        user = self.get_user(user_id)
        return user is not None and user.is_active

    def list_users(self) -> list[User]:
        """List all active users.

        Returns:
            list[User]: List of all active users.
        """
        session = self._get_session()
        try:
            return session.query(User).filter(User.is_active).all()
        finally:
            session.close()

    def count_users(self, role: UserRole | None = None, is_active: bool | None = None) -> int:
        """Count users, optionally filtered by role and/or active state."""
        session = self._get_session()
        try:
            query = session.query(User)
            if role is not None:
                query = query.filter(User.role == role)
            if is_active is not None:
                query = query.filter(User.is_active == is_active)
            return query.count()
        finally:
            session.close()

    def search_users(
        self,
        role: UserRole | None = None,
        is_active: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[int, list[dict]]:
        """Paginated user listing for the admin console.

        Returns ``(total, rows)`` where each row carries exactly the contract
        fields ``user_id, user_name, role, is_active, created_at,
        default_cube_id`` (never password material). Ordering is stable:
        ``created_at`` ascending, then ``user_name`` ascending.
        """
        session = self._get_session()
        try:
            query = session.query(User)
            if role is not None:
                query = query.filter(User.role == role)
            if is_active is not None:
                query = query.filter(User.is_active == is_active)
            total = query.count()
            users = (
                query.order_by(User.created_at.asc(), User.user_name.asc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            rows = []
            for user in users:
                default_cube = (
                    session.query(Cube)
                    .filter(Cube.owner_id == user.user_id)
                    .order_by(Cube.created_at.asc())
                    .first()
                )
                rows.append(
                    {
                        "user_id": user.user_id,
                        "user_name": user.user_name,
                        "role": user.role.value if hasattr(user.role, "value") else str(user.role),
                        "is_active": bool(user.is_active),
                        "created_at": user.created_at.isoformat() if user.created_at else None,
                        "default_cube_id": default_cube.cube_id if default_cube else None,
                    }
                )
            return total, rows
        finally:
            session.close()

    def update_user(
        self,
        user_id: str,
        role: UserRole | None = None,
        is_active: bool | None = None,
    ) -> bool:
        """Update role and/or active state of a user.

        Returns True when the user exists and the update was committed.
        """
        session = self._get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if user is None:
                return False
            if role is not None:
                user.role = role
            if is_active is not None:
                user.is_active = is_active
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error updating user: {e}")
            raise
        finally:
            session.close()

    def get_owned_cube_ids(self, user_id: str) -> list[str]:
        """IDs of cubes owned by the user, oldest first."""
        session = self._get_session()
        try:
            cubes = (
                session.query(Cube)
                .filter(Cube.owner_id == user_id)
                .order_by(Cube.created_at.asc())
                .all()
            )
            return [cube.cube_id for cube in cubes]
        finally:
            session.close()

    def get_default_cube_id(self, user_id: str) -> str | None:
        """The user's default cube: the earliest cube they own, if any."""
        owned = self.get_owned_cube_ids(user_id)
        return owned[0] if owned else None

    def list_active_cubes(self) -> list[dict]:
        """List active cubes (admin console assignment dialog)."""
        session = self._get_session()
        try:
            cubes = (
                session.query(Cube)
                .filter(Cube.is_active)
                .order_by(Cube.created_at.asc(), Cube.cube_name.asc())
                .all()
            )
            return [
                {
                    "cube_id": cube.cube_id,
                    "cube_name": cube.cube_name,
                    "owner_id": cube.owner_id,
                    "created_at": cube.created_at.isoformat() if cube.created_at else None,
                }
                for cube in cubes
            ]
        finally:
            session.close()

    def create_cube(
        self,
        cube_name: str,
        owner_id: str,
        cube_path: str | None = None,
        cube_id: str | None = None,
    ) -> str:
        """Create a new cube.

        Args:
            cube_name (str): Name of the cube.
            owner_id (str): ID of the cube owner.
            cube_path (str, optional): Path to the cube.
            cube_id (str, optional): Custom cube ID. If None, generates UUID.

        Returns:
            str: The created cube ID.

        Raises:
            ValueError: If owner doesn't exist.
        """
        session = self._get_session()
        try:
            # Validate owner exists
            owner = session.query(User).filter(User.user_id == owner_id).first()
            if not owner:
                raise ValueError(f"User with ID '{owner_id}' does not exist")

            cube = Cube(
                cube_name=cube_name,
                owner_id=owner_id,
                cube_path=cube_path,
                cube_id=cube_id or str(uuid.uuid4()),
            )
            session.add(cube)

            # Add owner to cube users
            cube.users.append(owner)

            session.commit()
            logger.info(f"Cube '{cube_name}' created with ID: {cube.cube_id}")
            return cube.cube_id
        except Exception as e:
            session.rollback()
            logger.error(f"Error creating cube: {e}")
            raise
        finally:
            session.close()

    def get_cube(self, cube_id: str) -> Cube | None:
        """Get cube by ID.

        Args:
            cube_id (str): The cube ID.

        Returns:
            Cube: The cube object or None if not found.
        """
        session = self._get_session()
        try:
            return session.query(Cube).filter(Cube.cube_id == cube_id).first()
        finally:
            session.close()

    def validate_user_cube_access(self, user_id: str, cube_id: str) -> bool:
        """Validate if a user has access to a cube.

        Args:
            user_id (str): The user ID.
            cube_id (str): The cube ID.

        Returns:
            bool: True if user has access to cube, False otherwise.
        """
        session = self._get_session()
        try:
            # Check if user exists and is active
            user = session.query(User).filter(User.user_id == user_id, User.is_active).first()
            if not user:
                return False

            # Check if cube exists and is active
            cube = session.query(Cube).filter(Cube.cube_id == cube_id, Cube.is_active).first()
            if not cube:
                return False

            # Check if user has access to cube (owner or in users list)
            if cube.owner_id == user_id:
                return True

            # Check many-to-many relationship
            return user in cube.users
        finally:
            session.close()

    def get_user_cubes(self, user_id: str) -> list[Cube]:
        """Get all cubes accessible by a user.

        Args:
            user_id (str): The user ID.

        Returns:
            list[Cube]: List of cubes accessible by the user.
        """
        session = self._get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user:
                return []

            active_cubes = [cube for cube in user.cubes if cube.is_active]
            return sorted(active_cubes, key=lambda cube: cube.created_at, reverse=True)
        finally:
            session.close()

    def add_user_to_cube(self, user_id: str, cube_id: str) -> bool:
        """Add a user to a cube's access list.

        Args:
            user_id (str): The user ID.
            cube_id (str): The cube ID.

        Returns:
            bool: True if successful, False otherwise.
        """
        session = self._get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            cube = session.query(Cube).filter(Cube.cube_id == cube_id).first()

            if not user or not cube:
                return False

            if user not in cube.users:
                cube.users.append(user)
                session.commit()
                logger.info(f"User '{user_id}' added to cube '{cube_id}'")

            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error adding user to cube: {e}")
            return False
        finally:
            session.close()

    def remove_user_from_cube(self, user_id: str, cube_id: str) -> bool:
        """Remove a user from a cube's access list.

        Args:
            user_id (str): The user ID.
            cube_id (str): The cube ID.

        Returns:
            bool: True if successful, False otherwise.
        """
        session = self._get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            cube = session.query(Cube).filter(Cube.cube_id == cube_id).first()

            if not user or not cube:
                return False

            # Don't remove owner
            if cube.owner_id == user_id:
                logger.warning(f"Cannot remove owner '{user_id}' from cube '{cube_id}'")
                return False

            if user in cube.users:
                cube.users.remove(user)
                session.commit()
                logger.info(f"User '{user_id}' removed from cube '{cube_id}'")

            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error removing user from cube: {e}")
            return False
        finally:
            session.close()

    def set_user_cubes(self, user_id: str, cube_ids: list[str]) -> bool:
        """Atomically replace the user's cube membership set.

        The whole diff (removals + additions) commits in a single
        transaction, so concurrent readers never observe a half-applied set.
        Owner memberships are never removed (an owner always keeps their own
        cube) and inactive/unknown cube IDs are skipped defensively — callers
        are expected to pre-validate the set and surface 4xx errors.

        Returns True when the user exists and the set was applied.
        """
        session = self._get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if user is None:
                return False

            wanted = set(cube_ids)
            current = {cube.cube_id for cube in user.cubes}

            for cube_id in current - wanted:
                cube = session.query(Cube).filter(Cube.cube_id == cube_id).first()
                if cube is None or cube.owner_id == user_id:
                    continue  # owner membership is permanent
                if cube in user.cubes:
                    user.cubes.remove(cube)

            for cube_id in wanted - current:
                cube = session.query(Cube).filter(Cube.cube_id == cube_id).first()
                if cube is not None and cube.is_active:
                    user.cubes.append(cube)

            session.commit()
            logger.info(f"Cube set for user '{user_id}' replaced with {len(wanted)} cube(s)")
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error replacing cube set: {e}")
            raise
        finally:
            session.close()

    def delete_user(self, user_id: str) -> bool:
        """Soft delete a user (set is_active to False).

        Args:
            user_id (str): The user ID.

        Returns:
            bool: True if successful, False otherwise.
        """
        session = self._get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user:
                return False

            # Don't delete root user
            if user.role == UserRole.ROOT:
                logger.warning("Cannot delete root user")
                return False

            user.is_active = False
            session.commit()
            logger.info(f"User '{user_id}' deactivated")
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error deleting user: {e}")
            return False
        finally:
            session.close()

    def delete_cube(self, cube_id: str) -> bool:
        """Soft delete a cube (set is_active to False).

        Args:
            cube_id (str): The cube ID.

        Returns:
            bool: True if successful, False otherwise.
        """
        session = self._get_session()
        try:
            cube = session.query(Cube).filter(Cube.cube_id == cube_id).first()
            if not cube:
                return False

            cube.is_active = False
            session.commit()
            logger.info(f"Cube '{cube_id}' deactivated")
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error deleting cube: {e}")
            return False
        finally:
            session.close()

    def close(self) -> None:
        """Close the database engine and dispose of all connections.

        This method should be called when the UserManager is no longer needed
        to ensure proper cleanup of database connections.
        """
        if hasattr(self, "engine"):
            self.engine.dispose()
            logger.info("UserManager database connections closed")
