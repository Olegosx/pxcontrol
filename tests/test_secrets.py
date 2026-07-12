"""Тесты шифрования секретов (ADR-0009). Подмена keyring — в conftest.py."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from pxcontrol.engine.db.models import Base, Bot
from pxcontrol.engine.security.secrets import get_secret_store


def test_encrypt_decrypt_roundtrip() -> None:
	"""Секрет расшифровывается в исходную строку, шифртекст отличается."""
	store = get_secret_store()
	secret = "123456:ABC-token"
	token = store.encrypt(secret)
	assert token != secret
	assert store.decrypt(token) == secret


def test_db_stores_ciphertext(tmp_path: Path) -> None:
	"""В файле БД лежит шифртекст, а ORM возвращает исходное значение."""
	engine = create_engine(f"sqlite:///{tmp_path / 'secrets.db'}")
	Base.metadata.create_all(engine)
	plain = "999999:XYZ-secret-token"
	with Session(engine) as session:
		session.add(Bot(label="тест", token=plain))
		session.commit()
	with Session(engine) as session:
		raw = session.execute(text("SELECT token FROM bots")).scalar_one()
		via_orm = session.execute(select(Bot)).scalar_one().token
	assert raw != plain, "секрет не должен лежать открытым текстом"
	assert via_orm == plain, "ORM должен прозрачно расшифровывать"
