"""Schema e operacoes do banco.

Duas tabelas:
  price_checks  - uma linha por companhia por checagem, com a resposta bruta.
  credit_usage  - ledger de creditos gastos (ver credits_tracker.py).

Nao existe coluna de "menor preco historico": ela e derivada com MIN() em
compare.py, para nao haver duas fontes de verdade.

Dois destinos possiveis, escolhidos pela presenca de `remote_url`:
  * SQLite local  - desenvolvimento, testes e execucao na maquina;
  * Turso/libSQL  - producao no GitHub Actions, onde o disco e efemero.

libSQL fala o dialeto do SQLite (placeholder `?`, `executescript`,
`AUTOINCREMENT`), entao o SQL e identico nos dois. A unica diferenca real e
que o cliente libSQL nao tem `row_factory` e devolve tuplas puras - por isso o
adaptador `Row` abaixo, que reconstroi o acesso por nome a partir de
`cursor.description`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence

from .models import Leg, Offer

logger = logging.getLogger(__name__)


class DBConnection(Protocol):
    """Contrato minimo que os modulos usam, atendido por sqlite3 e por libSQL."""

    def execute(self, sql: str, parameters: Sequence[Any] = ..., /) -> Any: ...

    def commit(self) -> None: ...

    def close(self) -> None: ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS price_checks (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at                TEXT    NOT NULL,
    airline                   TEXT    NOT NULL,
    price                     REAL    NOT NULL,
    currency                  TEXT    NOT NULL DEFAULT 'BRL',

    outbound_departure        TEXT,
    outbound_arrival          TEXT,
    outbound_duration_minutes INTEGER,
    outbound_stops            INTEGER,

    inbound_departure         TEXT,
    inbound_arrival           TEXT,
    inbound_duration_minutes  INTEGER,
    inbound_stops             INTEGER,

    fare_valid_until          TEXT,
    raw_response              TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_checks_airline_price
    ON price_checks (airline, price);

CREATE INDEX IF NOT EXISTS idx_price_checks_checked_at
    ON price_checks (checked_at DESC);

CREATE TABLE IF NOT EXISTS credit_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    year_month  TEXT    NOT NULL,
    credits     INTEGER NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    recorded_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credit_usage_year_month
    ON credit_usage (year_month);

-- Respostas que a API devolveu mas o parser nao entendeu. O credito ja foi
-- cobrado nesse ponto, entao guardar o payload e o que permite corrigir o
-- parser depois sem gastar credito de novo.
CREATE TABLE IF NOT EXISTS failed_extractions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at   TEXT NOT NULL,
    airline      TEXT NOT NULL,
    error        TEXT NOT NULL,
    raw_response TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------
# Adaptador libSQL -> interface do sqlite3
# --------------------------------------------------------------------------


class Row(Mapping):
    """Linha acessivel por indice e por nome, como o sqlite3.Row.

    O cliente libSQL devolve tuplas puras; os nomes vem de `cursor.description`
    (que preenche corretamente ate alias de agregacao, como `MIN(price) AS low`).
    """

    __slots__ = ("_values", "_columns")

    def __init__(self, values: Sequence[Any], columns: dict[str, int]) -> None:
        self._values = tuple(values)
        self._columns = columns

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        try:
            return self._values[self._columns[key]]
        except KeyError as exc:
            raise KeyError(f"coluna {key!r} nao esta no resultado") from exc

    def __iter__(self) -> Iterator[str]:
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        return list(self._columns)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._columns, self._values))})"


class _RowCursor:
    """Envolve o cursor do libSQL para devolver Row em vez de tupla."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    @property
    def description(self) -> Any:
        return self._cursor.description

    def _columns(self) -> dict[str, int]:
        return {col[0]: idx for idx, col in enumerate(self._cursor.description or ())}

    def fetchone(self) -> Row | None:
        row = self._cursor.fetchone()
        return None if row is None else Row(row, self._columns())

    def fetchall(self) -> list[Row]:
        columns = self._columns()
        return [Row(row, columns) for row in self._cursor.fetchall()]


class _RemoteConnection:
    """Conexao Turso/libSQL com a mesma superficie que o codigo usa do sqlite3."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> _RowCursor:
        return _RowCursor(self._conn.execute(sql, parameters))

    def executescript(self, sql: str) -> Any:
        return self._conn.executescript(sql)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


# --------------------------------------------------------------------------
# Conexao
# --------------------------------------------------------------------------


def _connect_remote(remote_url: str, auth_token: str | None) -> _RemoteConnection:
    try:
        import libsql
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise RuntimeError(
            "TURSO_DATABASE_URL esta definida mas o pacote 'libsql' nao esta "
            'instalado. Rode: pip install -e ".[turso]"'
        ) from exc

    logger.info("Conectando no banco remoto (Turso)")
    conn = libsql.connect(remote_url, auth_token=auth_token or "")
    return _RemoteConnection(conn)


def _connect_local(db_path: Path | str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # PRAGMAs so fazem sentido no arquivo local; no Turso o servidor cuida disso.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    logger.info("Conectado no SQLite local: %s", db_path)
    return conn


def connect(
    db_path: Path | str,
    remote_url: str | None = None,
    auth_token: str | None = None,
) -> DBConnection:
    """Abre a conexao e aplica o schema (idempotente).

    Com `remote_url` preenchida vai para o Turso; sem ela, SQLite local.
    """
    conn = _connect_remote(remote_url, auth_token) if remote_url else _connect_local(db_path)
    init_db(conn)
    return conn


def init_db(conn: Any) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def get_connection(
    db_path: Path | str,
    remote_url: str | None = None,
    auth_token: str | None = None,
) -> Iterator[DBConnection]:
    conn = connect(db_path, remote_url, auth_token)
    try:
        yield conn
    finally:
        conn.close()


def save_offer(conn: DBConnection, offer: Offer) -> int:
    """Grava uma oferta e devolve o id da linha criada."""
    outbound = offer.outbound
    inbound = offer.inbound
    cursor = conn.execute(
        """
        INSERT INTO price_checks (
            checked_at, airline, price, currency,
            outbound_departure, outbound_arrival,
            outbound_duration_minutes, outbound_stops,
            inbound_departure, inbound_arrival,
            inbound_duration_minutes, inbound_stops,
            fare_valid_until, raw_response
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            offer.checked_at,
            offer.airline,
            offer.price,
            offer.currency,
            outbound.departure if outbound else None,
            outbound.arrival if outbound else None,
            outbound.duration_minutes if outbound else None,
            outbound.stops if outbound else None,
            inbound.departure if inbound else None,
            inbound.arrival if inbound else None,
            inbound.duration_minutes if inbound else None,
            inbound.stops if inbound else None,
            offer.fare_valid_until,
            json.dumps(offer.raw_response, ensure_ascii=False),
        ),
    )
    conn.commit()
    row_id = int(cursor.lastrowid or 0)
    logger.info(
        "Oferta salva | id=%s | %s | %.2f %s", row_id, offer.airline, offer.price, offer.currency
    )
    return row_id


def save_failed_extraction(
    conn: DBConnection, airline: str, error: str, payload: Any
) -> int | None:
    """Guarda a resposta que o parser nao entendeu.

    Nunca propaga excecao: se ate isso falhar, o pipeline segue - o objetivo e
    salvar o que der, nao criar um segundo ponto de quebra.
    """
    from datetime import datetime, timezone

    try:
        cursor = conn.execute(
            """
            INSERT INTO failed_extractions (checked_at, airline, error, raw_response)
            VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                airline,
                error,
                json.dumps(payload, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
        row_id = int(cursor.lastrowid or 0)
        logger.info("Resposta bruta da %s salva em failed_extractions | id=%s", airline, row_id)
        return row_id
    except Exception:  # noqa: BLE001
        logger.exception("Nao consegui salvar a resposta bruta da %s", airline)
        return None


def get_failed_extraction(conn: DBConnection, extraction_id: int) -> dict[str, Any] | None:
    """Recupera um payload que falhou, para depurar o parser."""
    row = conn.execute(
        "SELECT raw_response FROM failed_extractions WHERE id = ?", (extraction_id,)
    ).fetchone()
    return None if row is None else json.loads(row["raw_response"])


def get_history(
    conn: DBConnection, airline: str | None = None, limit: int = 50
) -> list[Any]:
    """Historico mais recente primeiro, opcionalmente filtrado por companhia."""
    if airline:
        return conn.execute(
            """
            SELECT id, checked_at, airline, price, currency, fare_valid_until
            FROM price_checks
            WHERE airline = ?
            ORDER BY checked_at DESC
            LIMIT ?
            """,
            (airline, limit),
        ).fetchall()
    return conn.execute(
        """
        SELECT id, checked_at, airline, price, currency, fare_valid_until
        FROM price_checks
        ORDER BY checked_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def get_raw_response(conn: DBConnection, check_id: int) -> dict[str, Any] | None:
    """Recupera o JSON bruto de uma checagem, util se o schema da API mudar."""
    row = conn.execute(
        "SELECT raw_response FROM price_checks WHERE id = ?", (check_id,)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["raw_response"])


def count_checks(conn: DBConnection, airline: str | None = None) -> int:
    if airline:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM price_checks WHERE airline = ?", (airline,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM price_checks").fetchone()
    return int(row["n"])


def row_to_legs(row: Any) -> tuple[Leg | None, Leg | None]:
    """Reconstroi os trechos a partir de uma linha, para relatorios."""

    def build(prefix: str) -> Leg | None:
        if row[f"{prefix}_departure"] is None and row[f"{prefix}_duration_minutes"] is None:
            return None
        return Leg(
            origin="",
            destination="",
            departure=row[f"{prefix}_departure"],
            arrival=row[f"{prefix}_arrival"],
            duration_minutes=row[f"{prefix}_duration_minutes"],
            stops=row[f"{prefix}_stops"],
        )

    return build("outbound"), build("inbound")
