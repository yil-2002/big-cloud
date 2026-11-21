# ---------------------------------------------------------------------------
# hugecloud — ishlab chiqarish buyruqlari
# ---------------------------------------------------------------------------
PYTHON ?= python3
APP    ?= main.py
DB     ?= $(shell grep -E '^DATABASE_URL=' .env 2>/dev/null | cut -d= -f2-)

.PHONY: help install run bot api migrate test lint fmt compat docker-build docker-up docker-down \
        systemd-install systemd-restart logs clean archive

help:            ## Shu yordamni ko'rsatish
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:         ## Bog'liqliklarni o'rnatish
	$(PYTHON) -m pip install -r requirements.txt

run:             ## Bot + API bitta jarayonda (sukut rejim)
	$(PYTHON) $(APP) --mode all

bot:             ## Faqat bot (polling)
	$(PYTHON) $(APP) --mode bot

api:             ## Faqat HTTP API + Mini App
	$(PYTHON) $(APP) --mode api

migrate:         ## Sxemani yangilash (ma'lumot o'chmaydi)
	$(PYTHON) scripts/migrate.py

test:            ## Testlarni ishga tushirish
	$(PYTHON) -m pytest -q

lint:            ## Sintaksis + import + ruff (o'rnatilgan bo'lsa)
	$(PYTHON) -m compileall -q .
	@# `.env` bo'lmasa (CI/toza klon) soxta token bilan tekshiramiz — import xatosi
	@# konfiguratsiya yo'qligidan emas, haqiqiy sintaksis/ainanma muammosidan chiqsin.
	@if [ -f .env ]; then \
		$(PYTHON) -c "import api.app, bot.setup, main; print('imports OK')"; \
	else \
		BOT_TOKEN="1:lint-check" $(PYTHON) -c "import api.app, bot.setup, main; print('imports OK (no .env)')"; \
	fi
	@if $(PYTHON) -c "import ruff" 2>/dev/null || command -v ruff >/dev/null 2>&1; then \
		ruff check . && echo "ruff OK"; \
	else \
		echo "ruff o'rnatilmagan - o'tkazib yuborildi (pip install ruff)"; \
	fi

compat:          ## Python 3.11 (Docker obraz) sintaksis mosligi
	@if command -v ruff >/dev/null 2>&1; then \
		ruff check . --select ALL --target-version py311 --output-format concise | grep -i "invalid-syntax" && \
		(echo "XATO: 3.11'da ishlamaydigan sintaksis bor"; exit 1) || echo "py311 sintaksis OK"; \
	else \
		$(PYTHON) -m pytest tests/test_python_compat.py -q; \
	fi

fmt:             ## ruff format (ixtiyoriy)
	ruff format . && ruff check . --fix

docker-build:    ## Obrazni yig'ish
	docker compose build

docker-up:       ## To'liq stekni ishga tushirish (Postgres + Redis + app)
	docker compose up -d --build

docker-down:     ## To'xtatish
	docker compose down

systemd-install: ## systemd servisini o'rnatish (root, VPS uchun)
	sudo bash deploy/systemd-install.sh

systemd-restart: ## Servisni qayta ishga tushirish + logni ko'rsatish
	sudo systemctl restart hugecloud && sudo journalctl -u hugecloud -n 50 --no-pager

logs:            ## systemd loglarini kuzatish
	sudo journalctl -u hugecloud -n 100 -f

archive:         ## Loyihani tarqatish arxiviga yig'ish
	bash scripts/package.sh

clean:           ## Kesh va artefaktlarni tozalash
	find . -type d -name __pycache__ -prune -exec rm -rf {} + ; \
	find . -type d -name .pytest_cache -prune -exec rm -rf {} + ; \
	rm -rf .mypy_cache .ruff_cache htmlcov
