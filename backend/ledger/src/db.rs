//! Хранилище ledger.db — единственный владелец данных компаний (SQLite для стенда разработки; схема совместима по смыслу с PostgreSQL-сущностями из архитектуры).

use rusqlite::Connection;
use std::sync::{Arc, Mutex};

pub type Db = Arc<Mutex<Connection>>;

pub fn open(path: &str) -> Db {
    if let Some(dir) = std::path::Path::new(path).parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let conn = Connection::open(path).expect("open db");
    conn.execute_batch(
        "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS companies(
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            data_revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS imports(
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
            filename TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL,
            summary TEXT NOT NULL, payload TEXT, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS imports_company ON imports(company_id, digest);
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL REFERENCES companies(id),
            import_id TEXT NOT NULL, row_no INTEGER NOT NULL, date TEXT NOT NULL,
            amount_kop INTEGER NOT NULL, balance_kop INTEGER, counterparty TEXT NOT NULL,
            inn TEXT NOT NULL, purpose TEXT NOT NULL, category TEXT NOT NULL,
            category_conf REAL NOT NULL, manual INTEGER NOT NULL DEFAULT 0, dedup_key TEXT NOT NULL,
            UNIQUE(company_id, dedup_key));
        CREATE INDEX IF NOT EXISTS tx_company_date ON transactions(company_id, date);
        CREATE TABLE IF NOT EXISTS expected_events(
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
            date TEXT NOT NULL, amount_kop INTEGER NOT NULL, title TEXT NOT NULL,
            counterparty TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
            confidence REAL NOT NULL, created_at TEXT NOT NULL,
            external_id TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS events_external ON expected_events(company_id, external_id);
        CREATE TABLE IF NOT EXISTS audit_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
            company_id TEXT, action TEXT NOT NULL, object_id TEXT, outcome TEXT NOT NULL, request_id TEXT);",
    )
    .expect("migrate");
    // реквизиты компании (из сертификата ЭЦП) добавлены позже
    let cols: Vec<String> = {
        let mut q = conn.prepare("PRAGMA table_info(companies)").expect("table_info");
        let rows = q.query_map([], |r| r.get::<_, String>(1)).expect("table_info");
        rows.collect::<Result<_, _>>().expect("table_info")
    };
    for col in ["inn", "ogrn"] {
        if !cols.iter().any(|c| c == col) {
            conn.execute(&format!("ALTER TABLE companies ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"), []).expect("add column");
        }
    }
    // блокировка компании оператором платформы (метаданные; данные компании не затрагиваются)
    for col in ["banned_at", "ban_reason"] {
        if !cols.iter().any(|c| c == col) {
            conn.execute(&format!("ALTER TABLE companies ADD COLUMN {col} TEXT"), []).expect("add column");
        }
    }
    // роли: участники компании. Директор — владелец (owner_id); у баз до появления ролей запись директора создаётся здесь.
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS company_members(
            company_id TEXT NOT NULL REFERENCES companies(id), user_id TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL DEFAULT '', role TEXT NOT NULL CHECK(role IN ('director','editor','viewer')),
            created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS members_company ON company_members(company_id);
        CREATE UNIQUE INDEX IF NOT EXISTS members_one_director ON company_members(company_id) WHERE role='director';
        INSERT OR IGNORE INTO company_members(company_id, user_id, email, role, created_at, created_by)
            SELECT id, owner_id, '', 'director', created_at, '' FROM companies;",
    )
    .expect("migrate members");
    Arc::new(Mutex::new(conn))
}
