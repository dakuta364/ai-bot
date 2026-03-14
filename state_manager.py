import sqlite3
import json
import os
import logging
import time
from threading import Lock

DB_FILE = "bot.db"
JSON_STATE_FILE = "bot_state.json"
HISTORY_LIMIT = 20

class StateManager:
    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self.lock = Lock()
        self._init_db()
        self._migrate_from_json()
        self.last_cleanup = time.time()
        self._cleanup_expired_sessions()

    def _get_conn(self):
        """Creates a SQLite connection with proper settings."""
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging for better concurrency
        return conn

    def _init_db(self):
        """Creates tables if they don't exist."""
        with self.lock:
            conn = self._get_conn()
            try:
                cursor = conn.cursor()
                # Users table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        policy_accepted BOOLEAN DEFAULT 0,
                        last_interaction REAL DEFAULT 0
                    )
                ''')
                # Messages table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        role TEXT,
                        content TEXT,
                        timestamp REAL,
                        FOREIGN KEY(user_id) REFERENCES users(user_id)
                    )
                ''')
                # Pending bookings table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS pending_bookings (
                        user_id INTEGER PRIMARY KEY,
                        data TEXT,
                        FOREIGN KEY(user_id) REFERENCES users(user_id)
                    )
                ''')
                conn.commit()
            except Exception as e:
                logging.error(f"❌ Database init failed: {e}")
            finally:
                conn.close()

    def _migrate_from_json(self):
        """Migrates data from bot_state.json if DB is empty."""
        if not os.path.exists(JSON_STATE_FILE):
            return

        with self.lock:
            conn = self._get_conn()
            try:
                cursor = conn.cursor()
                # Check if DB is empty
                cursor.execute("SELECT count(*) FROM users")
                if cursor.fetchone()[0] > 0:
                    return # Already populated

                logging.info(f"📦 Migrating data from {JSON_STATE_FILE} to SQLite...")
                
                with open(JSON_STATE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                # Migrate User Meta
                user_meta = data.get("user_meta", {})
                for uid, meta in user_meta.items():
                    cursor.execute(
                        "INSERT OR IGNORE INTO users (user_id, policy_accepted, last_interaction) VALUES (?, ?, ?)",
                        (uid, meta.get("policy_accepted", False), meta.get("last_interaction", 0))
                    )

                # Migrate Histories
                histories = data.get("histories", {})
                for uid, msgs in histories.items():
                    # Ensure user exists (if meta was missing but history exists)
                    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
                    for msg in msgs:
                        cursor.execute(
                            "INSERT INTO messages (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                            (uid, msg.get('role'), msg.get('content'), time.time())
                        )

                # Migrate Pending Bookings
                pending = data.get("pending_bookings", {})
                for uid, booking_data in pending.items():
                    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
                    cursor.execute(
                        "INSERT OR REPLACE INTO pending_bookings (user_id, data) VALUES (?, ?)",
                        (uid, json.dumps(booking_data, ensure_ascii=False))
                    )
                
                conn.commit()
                logging.info("✅ Migration completed successfully!")
                
                # Rename JSON to indicate it's archived
                # os.rename(JSON_STATE_FILE, JSON_STATE_FILE + ".bak") 

            except Exception as e:
                logging.error(f"❌ Migration failed: {e}")
            finally:
                conn.close()

    def _cleanup_expired_sessions(self):
        """Removes data for users inactive for > 30 minutes."""
        with self.lock:
            conn = self._get_conn()
            try:
                cursor = conn.cursor()
                limit_time = time.time() - 1800 # 30 mins
                
                # Get expired users
                cursor.execute("SELECT user_id FROM users WHERE last_interaction < ? AND last_interaction > 0", (limit_time,))
                expired_users = [row[0] for row in cursor.fetchall()]

                if expired_users:
                    # Clear history and pending bookings
                    cursor.executemany("DELETE FROM messages WHERE user_id = ?", [(uid,) for uid in expired_users])
                    cursor.executemany("DELETE FROM pending_bookings WHERE user_id = ?", [(uid,) for uid in expired_users])
                    conn.commit()
                    logging.info(f"🧹 Privacy cleanup: Cleared sessions for {len(expired_users)} users.")
            
            except Exception as e:
                logging.error(f"Cleanup failed: {e}")
            finally:
                conn.close()

    # --- Meta & Policy Management ---
    def get_user_meta(self, user_id):
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT policy_accepted, last_interaction FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            conn.close()
            
            if row:
                return {"policy_accepted": bool(row[0]), "last_interaction": row[1]}
            return {}

    def update_last_interaction(self, user_id):
        with self.lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO users (user_id, last_interaction) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET last_interaction=excluded.last_interaction",
                (user_id, time.time())
            )
            conn.commit()
            conn.close()

        # 🧹 Periodic Cleanup (Once per hour)
        if time.time() - self.last_cleanup > 3600:
            self._cleanup_expired_sessions()

    def is_policy_accepted(self, user_id):
        meta = self.get_user_meta(user_id)
        return meta.get("policy_accepted", False)

    def set_policy_accepted(self, user_id):
        with self.lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO users (user_id, policy_accepted, last_interaction) VALUES (?, 1, ?) ON CONFLICT(user_id) DO UPDATE SET policy_accepted=1, last_interaction=?",
                (user_id, time.time(), time.time())
            )
            conn.commit()
            conn.close()

    # --- History Management ---
    def get_history(self, user_id):
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT role, content FROM messages WHERE user_id = ? ORDER BY id ASC", (user_id,))
            rows = cursor.fetchall()
            conn.close()
            return [{"role": r[0], "content": r[1]} for r in rows]

    def update_history(self, user_id, history):
        # Full replace strategy for compatibility is inefficient in SQL.
        # Ideally, we append ONE message. But the bot logic passes the Whole List.
        # Optimization: Delete all and re-insert (easiest migration) OR diff (complex).
        # Given N=20, delete/insert is fast enough (~1-2ms).
        
        with self.lock:
            conn = self._get_conn()
            try:
                # Ensure user exists
                conn.execute("INSERT OR IGNORE INTO users (user_id, last_interaction) VALUES (?, ?)", (user_id, time.time()))
                
                # Transaction
                conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
                
                # Bulk insert
                # Используем оригинальное время, если есть
                data = []
                for msg in history:
                    original_time = msg.get('timestamp', time.time())
                    content = msg.get('content', '') or ''
                    data.append((user_id, msg.get('role'), content, original_time))
                
                conn.executemany("INSERT INTO messages (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)", data)
                
                # Update interaction
                conn.execute("UPDATE users SET last_interaction = ? WHERE user_id = ?", (time.time(), user_id))
                
                conn.commit()
            except Exception as e:
                logging.error(f"History update failed: {e}")
            finally:
                conn.close()

    def clear_history(self, user_id):
        with self.lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()

    # --- Booking Management ---
    def get_pending_booking(self, user_id):
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM pending_bookings WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return json.loads(row[0])
            return None

    def set_pending_booking(self, user_id, booking_data):
        with self.lock:
            conn = self._get_conn()
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            conn.execute(
                "INSERT OR REPLACE INTO pending_bookings (user_id, data) VALUES (?, ?)",
                (user_id, json.dumps(booking_data, ensure_ascii=False))
            )
            conn.commit()
            conn.close()

    def clear_pending_booking(self, user_id):
        with self.lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM pending_bookings WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()

    def clear_user_state(self, user_id):
        """Clears functional state but KEEPS meta (policy acceptance)."""
        with self.lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM pending_bookings WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
