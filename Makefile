STACK_NAME ?= kurukin-asset-hub
WEB_IMAGE ?= kurukin-asset-hub-web:bootstrap
WEB_SERVICE ?= $(STACK_NAME)_web

.PHONY: build deploy rm logs ps shell migrate revision test

build:
	docker build -t $(WEB_IMAGE) .

deploy:
	set -a; . ./.env; set +a; docker stack deploy -c docker-compose.yml $(STACK_NAME)

rm:
	docker stack rm $(STACK_NAME)

logs:
	docker service logs $(WEB_SERVICE) --tail 100 -f

ps:
	docker stack ps $(STACK_NAME)

shell:
	docker run --rm -it --env-file .env --network $(STACK_NAME)_asset_hub_internal $(WEB_IMAGE) /bin/sh

migrate:
	docker run --rm --env-file .env --network $(STACK_NAME)_asset_hub_internal $(WEB_IMAGE) alembic upgrade head

revision:
	docker run --rm -it --env-file .env -v "$$(pwd)/alembic/versions:/app/alembic/versions" $(WEB_IMAGE) alembic revision --autogenerate -m "$(m)"

test:
	docker run --rm $(WEB_IMAGE) pytest
