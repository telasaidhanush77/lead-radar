# Windows (no make): run the command on the right-hand side directly.
up:
	docker compose up --build

test:
	pytest -q

lint:
	ruff check .

bench:
	python -m bench.run
