#!/usr/bin/env python3
"""archmap: what each repository (or monorepo service) is built with and what it connects to.

Static and evidence-based: every fact carries the file and line it came from.

  python archmap.py              print the map as text (same as `teammap arch`)
  python archmap.py <repo>       one repository in detail
  python archmap.py --json       the whole map as JSON (what /api/arch serves)
  python archmap.py --mermaid    the system map as a Mermaid diagram
  python archmap.py <name>       a repo/service in detail, or a component (rabbitmq, mongodb…): who uses it
  python archmap.py impact <svc> who calls it, and who calls those (what a change to it can break)
  python archmap.py path <a> <b> how a call gets from a to b (the HTTP hops, with evidence)
  Add --json for machine-readable output. Results are cached in <team>/graph/index/arch.json until a file changes.

Units: the git repositories directly under the workspace or, when the workspace is itself one git
repository (a monorepo), every top-level folder with a build file (pom.xml, package.json, …).

What it reads: dependency files (requirements, pyproject, package.json, pom.xml, Gradle), code
(.py .java .kt .ts .js …), Spring config (application*.yml / .properties, bootstrap.yml),
Dockerfiles, compose files, CI files and .env.example files. It never opens .env files or anything
under a secrets/ folder, keeps only the *names* of environment variables, and masks credentials
in the evidence lines it shows.

Confidence of a component: "declared" (only a dependency), "used" (the code uses it),
"wired" (also configured: an env var, a property, a compose service, a connection URL).
Links between services: "exact" (a Feign client, a gateway route or a URL naming the service),
"inferred" (a client class or env var whose name looks like the service's).
"""
import json
import os
import re
import sys
import time

if __name__ == "__main__" and not __package__:  # run as a file: python src/aiset/archmap.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    __package__ = "aiset"
from .workspace import CODE_ROOT, GRAPHS, norm  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))

SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "env", "fastapi-env", "__pycache__", "dist", "build", ".next",
             "logs", "output", "dumps", "site-packages", ".mypy_cache", ".pytest_cache", ".ruff_cache", "coverage",
             "secrets", ".idea", ".vscode", "vendor", "target", ".gradle", "bin", "obj", "out"}
UNIT_FILES = ("pom.xml", "build.gradle", "build.gradle.kts", "package.json", "requirements.txt", "pyproject.toml",
              "setup.py", "go.mod", "Cargo.toml", "composer.json", "Gemfile", "Dockerfile")
CODE_EXT = {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
            ".mjs": "JavaScript", ".java": "Java", ".kt": "Kotlin", ".go": "Go", ".rb": "Ruby", ".php": "PHP",
            ".cs": "C#", ".rs": "Rust", ".scala": "Scala", ".sql": "SQL", ".sh": "Shell"}
MAX_FILE = 512 * 1024
MAX_EVIDENCE = 6

# ---- detectors ---------------------------------------------------------------------------------
# py / npm / jvm: dependency names (jvm = Maven/Gradle artifactId); code: regexes over code lines;
# prop: regexes over Spring-style "key=value" config entries; env: regexes over env var names;
# image: regexes over compose/k8s image names. kind decides the column on the map.
DETECTORS = [
    dict(id="postgres", label="PostgreSQL", kind="database",
         py=["psycopg2", "psycopg2-binary", "psycopg", "asyncpg", "pg8000"], npm=["pg", "postgres", "pg-promise"], jvm=["postgresql", "r2dbc-postgresql"],
         go=["github.com/jackc/pgx", "github.com/lib/pq", "gorm.io/driver/postgres"],
         code=[r"postgres(?:ql)?(?:\+\w+)?://", r"\basyncpg\.(?:connect|create_pool)", r"\bpsycopg2?\.connect", r"jdbc:postgresql:"],
         prop=[r"jdbc:postgresql:", r"database\s*=\s*postgres", r"PostgreSQL\w*Dialect"], env=[r"^(POSTGRES|PG)_?[A-Z_]*$", r"DATABASE_URL$"], image=[r"postgres|postgis|timescale"]),
    dict(id="mysql", label="MySQL", kind="database", py=["pymysql", "mysqlclient", "aiomysql"], npm=["mysql", "mysql2"],
         jvm=["mysql-connector-java", "mysql-connector-j", "mariadb-java-client"],
         go=["github.com/go-sql-driver/mysql", "gorm.io/driver/mysql"],
         code=[r"mysql(?:\+\w+)?://", r"jdbc:mysql:"], prop=[r"jdbc:mysql:", r"database\s*=\s*mysql", r"MySQL\w*Dialect", r"com\.mysql\."],
         env=[r"^MYSQL_"], image=[r"mysql|mariadb"]),
    dict(id="mongodb", label="MongoDB", kind="database", py=["pymongo", "motor", "mongoengine", "flask-pymongo", "mongoalchemy", "flask-mongoalchemy"],
         npm=["mongodb", "mongoose", "@nestjs/mongoose", "winston-mongodb"], jvm=["spring-boot-starter-data-mongodb", "mongodb-driver", "mongodb-driver-sync", "mongo-java-driver"],
         go=["go.mongodb.org/mongo-driver"],
         code=[r"mongodb(?:\+srv)?://", r"\bMongo(?:Template|Repository|Client)\b", r"\bmongoose\.(?:connect|model|Schema)\b"],
         prop=[r"^spring\.data\.mongodb\.", r"mongodb://"], env=[r"^MONGO"], image=[r"mongo"]),
    dict(id="cassandra", label="Cassandra", kind="database", py=["cassandra-driver"], npm=["cassandra-driver"],
         jvm=["spring-boot-starter-data-cassandra", "cassandra-driver-core", "java-driver-core"],
         code=[r"^\s*(?:from|import)\s+cassandra\b", r"\b(?:CassandraTemplate|CqlSession)\b"], prop=[r"^spring\.(?:data\.)?cassandra\."],
         env=[r"^CASSANDRA"], image=[r"cassandra|scylla"]),
    dict(id="sqlite", label="SQLite", kind="database", py=["aiosqlite"], npm=["sqlite3", "better-sqlite3"], jvm=["sqlite-jdbc"],
         code=[r"sqlite(?:\+\w+)?:///", r"\bsqlite3\.connect"], prop=[r"jdbc:sqlite:"], env=[], image=[]),
    dict(id="redis", label="Redis", kind="cache", py=["redis", "aioredis", "redis-py-cluster", "flask-redis"], npm=["redis", "ioredis"],
         jvm=["spring-boot-starter-data-redis", "spring-data-redis", "jedis", "lettuce-core", "redisson"],
         go=["github.com/redis/go-redis", "github.com/go-redis/redis"],
         code=[r"redis(?:s)?://", r"^\s*(?:import|from)\s+(?:redis|aioredis)\b", r"\bRedis\(", r"\b(?:RedisTemplate|StringRedisTemplate|Jedis(?:Pool)?)\b"],
         prop=[r"^spring\.(?:data\.)?redis\."], env=[r"^REDIS"], image=[r"redis|valkey"]),
    dict(id="celery", label="Celery", kind="worker", py=["celery"], npm=[], jvm=[], only=(".py",),
         code=[r"\bCelery\(", r"@(?:shared_task|\w+\.task)\b", r"\.delay\(|\.apply_async\("], prop=[], env=[r"^CELERY"], image=[]),
    dict(id="rabbitmq", label="RabbitMQ", kind="broker", py=["pika", "aio-pika", "kombu", "amqp", "aiormq"],
         npm=["amqplib", "amqp-connection-manager", "amqplib-as-promised", "@golevelup/nestjs-rabbitmq"], jvm=["spring-rabbit", "spring-boot-starter-amqp", "amqp-client"],
         go=["github.com/rabbitmq/amqp091-go", "github.com/streadway/amqp"],
         code=[r"amqps?://", r"^\s*(?:import|from)\s+(?:pika|aio_pika|kombu)\b", r"\bbasic_(?:publish|consume)\b", r"\b(?:RabbitTemplate|AmqpTemplate)\b|@RabbitListener",
               r"\bamqp\.connect\(|require\(['\"]amqplib"],
         prop=[r"^spring\.rabbitmq\."], env=[r"^RABBIT", r"^AMQP"], image=[r"rabbitmq"]),
    dict(id="kafka", label="Kafka", kind="broker", py=["kafka-python", "aiokafka", "confluent-kafka"], npm=["kafkajs", "node-rdkafka"],
         jvm=["spring-kafka", "kafka-clients", "kafka-streams"],
         go=["github.com/segmentio/kafka-go", "github.com/IBM/sarama", "github.com/Shopify/sarama", "github.com/confluentinc/confluent-kafka-go"],
         code=[r"^\s*(?:import|from)\s+(?:kafka|aiokafka|confluent_kafka)\b", r"\bKafka(?:Producer|Consumer|Template)\b|@KafkaListener", r"new Kafka\("],
         prop=[r"^spring\.kafka\.", r"bootstrap[.-]servers"], env=[r"^KAFKA"], image=[r"kafka|redpanda"]),
    dict(id="mqtt", label="MQTT", kind="broker", py=["paho-mqtt", "asyncio-mqtt", "gmqtt"], npm=["mqtt", "async-mqtt"],
         jvm=["org.eclipse.paho.client.mqttv3", "spring-integration-mqtt", "hivemq-mqtt-client"],
         code=[r"mqtts?://|tcp://[\w.-]+:1883", r"\bMqtt(?:Client|AsyncClient)\b", r"\bmqtt\.connect\("], prop=[r"^\w*\.?mqtt\.|mqtt"],
         env=[r"^MQTT"], image=[r"mosquitto|emqx|hivemq|vernemq"]),
    dict(id="keycloak", label="Keycloak", kind="auth", py=["python-keycloak"], npm=["keycloak-js", "@react-keycloak/web", "keycloak-connect"],
         jvm=["keycloak-spring-boot-starter", "keycloak-spring-security-adapter", "keycloak-admin-client"],
         code=[r"\bKeycloak(?:OpenID)?\(", r"/realms/"], prop=[r"^keycloak\.", r"/realms/"], env=[r"^KEYCLOAK", r"(?:JWT_ISSUER|OIDC|AUTH)_[A-Z_]*URL$"], image=[r"keycloak"]),
    dict(id="jwt", label="OAuth2 / JWT", kind="auth", py=["pyjwt", "python-jose", "authlib"], npm=["jsonwebtoken", "jose", "jwt-decode", "passport-jwt"],
         jvm=["spring-cloud-starter-oauth2", "spring-security-oauth2", "spring-boot-starter-oauth2-resource-server", "spring-security-jwt", "jjwt", "jjwt-api", "java-jwt", "nimbus-jose-jwt"],
         code=[r"^\s*(?:import|from)\s+(?:jwt|jose)\b", r"\bjwt\.(?:decode|verify|sign)\(", r"@EnableResourceServer|@EnableAuthorizationServer|\bJwtDecoder\b"],
         prop=[r"^security\.oauth2\.", r"^spring\.security\.oauth2\.", r"issuer-uri|jwk-set-uri"], env=[r"^JWT_"], image=[]),
    dict(id="s3", label="Amazon S3", kind="storage", py=["boto3", "aioboto3"], npm=["@aws-sdk/client-s3", "aws-sdk"], jvm=["aws-java-sdk", "aws-java-sdk-s3", "s3"],
         code=[r"boto3\.(?:client|resource)\(['\"]s3", r"\bAmazonS3(?:Client)?\b|\bS3Client\b"], prop=[r"^(?:cloud\.)?aws\.s3|amazonaws\.com"], env=[r"^S3_", r"^AWS_S3"], image=[]),
    dict(id="gcs", label="Google Cloud Storage", kind="storage", py=["google-cloud-storage"], npm=["@google-cloud/storage"], jvm=["google-cloud-storage"],
         code=[r"storage\.Client\(", r"\bStorageOptions\b"], prop=[], env=[r"^GCS_", r"^GOOGLE_CLOUD_STORAGE"], image=[]),
    dict(id="minio", label="MinIO", kind="storage", py=["minio"], npm=["minio"], jvm=["minio"], code=[r"\bMinio(?:Client)?\(|new Minio\.Client|MinioClient\.builder"],
         prop=[r"^minio\."], env=[r"^MINIO"], image=[r"minio"]),
    dict(id="bigquery", label="BigQuery", kind="database", py=["google-cloud-bigquery"], npm=["@google-cloud/bigquery"], jvm=["google-cloud-bigquery"],
         code=[r"bigquery\.Client\("], prop=[], env=[r"^BIGQUERY"], image=[]),
    dict(id="elasticsearch", label="Elasticsearch", kind="search", py=["elasticsearch", "opensearch-py"], npm=["@elastic/elasticsearch"],
         jvm=["spring-boot-starter-data-elasticsearch", "elasticsearch-java", "elasticsearch-rest-high-level-client"],
         code=[r"\bElasticsearch(?:Client)?\(|RestHighLevelClient"], prop=[r"^spring\.elasticsearch\.|^spring\.data\.elasticsearch\."], env=[r"^(ELASTIC|OPENSEARCH)"], image=[r"elasticsearch|opensearch"]),
    dict(id="sentry", label="Sentry", kind="observability", py=["sentry-sdk"], npm=["@sentry/react", "@sentry/node", "@sentry/browser"], jvm=["sentry-spring-boot-starter", "sentry"],
         code=[r"sentry_sdk\.init\(|Sentry\.init\("], prop=[r"^sentry\."], env=[r"^SENTRY"], image=[]),
    dict(id="prometheus", label="Prometheus metrics", kind="observability", py=["prometheus-client", "prometheus-flask-exporter", "prometheus-fastapi-instrumentator"],
         npm=["prom-client", "express-prom-bundle"], jvm=["micrometer-registry-prometheus", "simpleclient", "simpleclient_spring_boot", "simpleclient_hotspot", "simpleclient_servlet"],
         code=[r"PrometheusMetrics\(|@EnablePrometheusEndpoint|collectDefaultMetrics\("], prop=[r"prometheus"], env=[], image=[r"prometheus"]),
    dict(id="airflow", label="Airflow", kind="worker", py=["apache-airflow"], npm=[], jvm=[], only=(".py",),
         code=[r"^\s*(?:from|import)\s+airflow\b", r"\bDAG\("], prop=[], env=[r"^AIRFLOW"], image=[r"airflow"]),
    dict(id="scheduler", label="Scheduled jobs", kind="worker", py=["apscheduler", "schedule", "rq-scheduler", "flask-apscheduler", "fastapi-scheduler"],
         npm=["node-cron", "cron", "agenda", "@nestjs/schedule", "node-schedule"], jvm=["quartz", "spring-boot-starter-quartz", "shedlock-spring"],
         code=[r"\bBackgroundScheduler\(|\bAsyncIOScheduler\(|cron\.schedule\(", r"@Scheduled\b|@EnableScheduling", r"new Agenda\(|agenda\.(?:every|define)\(", r"@Cron\("],
         prop=[], env=[], image=[]),
    dict(id="smtp", label="Email (SMTP)", kind="external", py=["aiosmtplib", "fastapi-mail", "flask-mail"], npm=["nodemailer"], jvm=["spring-boot-starter-mail", "javax.mail", "jakarta.mail"],
         code=[r"\bsmtplib\.SMTP", r"\bJavaMailSender\b", r"createTransport\("], prop=[r"^spring\.mail\."], env=[r"^(SMTP|MAIL)_"], image=[]),
    dict(id="websocket", label="WebSocket", kind="external", py=["websockets", "python-socketio", "flask-socketio"], npm=["socket.io-client", "socket.io", "ws", "@nestjs/websockets"],
         jvm=["spring-boot-starter-websocket"], code=[r"@\w+\.websocket\(|new WebSocket\(|io\(['\"]", r"@ServerEndpoint|@MessageMapping|SimpMessagingTemplate|@WebSocketGateway"],
         prop=[], env=[], image=[]),
]
FRAMEWORKS_PY = [("fastapi", "FastAPI", "api"), ("django", "Django", "api"), ("flask", "Flask", "api"), ("cherrypy", "CherryPy", "api"),
                 ("starlette", "Starlette", "api"), ("apache-airflow", "Airflow", "pipeline"), ("streamlit", "Streamlit", "frontend")]
FRAMEWORKS_NPM = [("next", "Next.js", "frontend"), ("react", "React", "frontend"), ("vue", "Vue", "frontend"),
                  ("@angular/core", "Angular", "frontend"), ("svelte", "Svelte", "frontend"),
                  ("@nestjs/core", "NestJS", "api"), ("express", "Express", "api"), ("fastify", "Fastify", "api"), ("koa", "Koa", "api")]
# order matters: the most specific role wins (a gateway is also a Spring Boot web app)
FRAMEWORKS_JVM = [("spring-cloud-starter-zuul", "Zuul gateway", "gateway"), ("spring-cloud-starter-netflix-zuul", "Zuul gateway", "gateway"),
                  ("spring-cloud-starter-gateway", "Spring Cloud Gateway", "gateway"),
                  ("spring-cloud-starter-eureka-server", "Eureka server", "registry"), ("spring-cloud-starter-netflix-eureka-server", "Eureka server", "registry"),
                  ("spring-cloud-config-server", "Config server", "registry"),
                  ("spring-boot-starter-web", "Spring Boot", "api"), ("spring-boot-starter-webflux", "Spring Boot (WebFlux)", "api"),
                  ("ktor-server-core", "Ktor", "api"), ("quarkus-core", "Quarkus", "api"), ("micronaut-http-server", "Micronaut", "api"),
                  ("spring-boot-starter", "Spring Boot", "service")]
# Go module paths without a /vN major suffix (_deps strips it), so echo/v5 or fiber/v3 match too
FRAMEWORKS_GO = [("github.com/gin-gonic/gin", "Gin", "api"), ("github.com/labstack/echo", "Echo", "api"),
                 ("github.com/go-chi/chi", "Chi", "api"), ("github.com/gofiber/fiber", "Fiber", "api"),
                 ("github.com/gorilla/mux", "Gorilla Mux", "api"), ("github.com/kataras/iris", "Iris", "api")]
CODE_ROLE = [(r"@EnableZuulProxy", "Zuul gateway", "gateway"), (r"@EnableEurekaServer", "Eureka server", "registry"),
             (r"@EnableConfigServer", "Config server", "registry")]
# libraries worth naming on a repo card
NOTABLE = {"sqlalchemy": "SQLAlchemy", "sqlmodel": "SQLModel", "alembic": "Alembic", "pydantic": "Pydantic",
           "pandas": "pandas", "numpy": "NumPy", "ortools": "OR-Tools", "pulp": "PuLP", "scikit-learn": "scikit-learn",
           "torch": "PyTorch", "openpyxl": "openpyxl", "uvicorn": "Uvicorn", "gunicorn": "Gunicorn", "httpx": "httpx",
           "requests": "requests", "aiohttp": "aiohttp", "vite": "Vite", "axios": "axios", "@mui/material": "MUI",
           "antd": "Ant Design", "tailwindcss": "Tailwind", "@tanstack/react-query": "React Query", "redux": "Redux",
           "@reduxjs/toolkit": "Redux Toolkit", "zustand": "Zustand", "i18next": "i18next", "leaflet": "Leaflet",
           "maplibre-gl": "MapLibre", "@deck.gl/core": "deck.gl", "typescript": "TypeScript", "prisma": "Prisma", "typeorm": "TypeORM",
           "mongoose": "Mongoose", "inversify": "InversifyJS", "swagger-ui-express": "Swagger", "@nestjs/swagger": "Swagger",
           "spring-boot-starter-data-jpa": "Spring Data JPA", "hibernate-core": "Hibernate", "spring-cloud-starter-feign": "Feign",
           "spring-cloud-starter-openfeign": "OpenFeign", "spring-cloud-starter-eureka": "Eureka client",
           "spring-cloud-starter-netflix-eureka-client": "Eureka client", "spring-cloud-starter-hystrix": "Hystrix",
           "resilience4j-spring-boot2": "Resilience4j", "springfox-swagger2": "Swagger", "springdoc-openapi-ui": "OpenAPI",
           "lombok": "Lombok", "spring-boot-starter-actuator": "Actuator", "spring-cloud-starter-config": "Config client",
           "eureka-js-client": "Eureka client", "flask-marshmallow": "marshmallow", "shapely": "Shapely", "jts": "JTS"}
HTTP_LIBS_PY, HTTP_LIBS_NPM = {"requests", "httpx", "aiohttp"}, {"axios", "ky", "got", "node-fetch", "request", "request-promise"}
HTTP_LIBS_JVM = {"spring-cloud-starter-feign", "spring-cloud-starter-openfeign", "okhttp", "httpclient", "spring-boot-starter-webflux", "retrofit"}
EUREKA_CLIENT = {"spring-cloud-starter-eureka", "spring-cloud-starter-netflix-eureka-client", "eureka-js-client", "py-eureka-client"}
INFRA_ENV = re.compile(r"DATABASE|DB_|POSTGRES|^PG|REDIS|CELERY|RABBIT|AMQP|KAFKA|BROKER|SENTRY|SMTP|MAIL|S3|GCS|MINIO|MONGO|MYSQL|ELASTIC|SWAGGER|REDOC|DOCS_|STATIC|CALLBACK|REDIRECT|LOG|CORS|FRONTEND|EUREKA|MQTT|CASSANDRA")
URL_ENV = re.compile(r"(?:_URL|_URI|_HOST|_BASE_URL|_ENDPOINT|_API|_ADDR)$")
# client classes for infrastructure (RabbitMQClient, MinioClient…) are components, not services
INFRA_WORDS = {"rabbit", "rabbitmq", "mq", "mqclient", "amqp", "pika", "minio", "redis", "kafka", "s3", "gcs", "storage", "db",
               "database", "postgres", "sql", "mongo", "keycloak", "sentry", "celery", "smtp", "mail", "email", "http", "base",
               "abstract", "generic", "mock", "fake", "test", "websocket", "socket", "auth", "jwt", "cache", "logger", "airflow",
               "mqtt", "cassandra", "eureka", "rest", "web", "feign", "null", "noop", "dummy", "default", "impl", "stub", "local"}
GENERIC = {"service", "services", "backend", "frontend", "web", "api", "app", "new", "server", "client", "the", "and", "base", "url", "uri",
           "host", "endpoint", "addr", "microservice", "manager", "management"}
NOT_HOSTS = {"localhost", "host", "example", "www", "127", "0", "your", "server", "api", "app", "schemas", "json", "xml"}

SECRET_RE = re.compile(r"((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)\w*[\"']?\s*[:=]\s*)(['\"]?)[^\s'\",)]+", re.I)
CRED_URL_RE = re.compile(r"(\w+://)[^/\s:@'\"]+:[^@\s'\"]+@")
COMMENT_RE = re.compile(r"^\s*(?://|#|\*|/\*|<!--)")


def mask(line):
    line = CRED_URL_RE.sub(r"\1***:***@", line)
    return SECRET_RE.sub(r"\1\2***", line).strip()[:180]


def _read(path):
    try:
        if os.path.getsize(path) > MAX_FILE:
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _tokens(name):
    """BackendServiceClient / BACKEND_URL / backend -> {'backend', …}"""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    s = re.sub(r"([A-Za-z])(V\d+)\b", r"\1_\2", s)
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if t}


def _key(name):
    """Service names compare without case, dashes or underscores: Traffic_Telemetry == traffic-telemetry."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def is_monorepo():
    try:
        names = os.listdir(CODE_ROOT)
    except OSError:
        return False
    return os.path.isdir(os.path.join(CODE_ROOT, ".git")) and not any(
        os.path.isdir(os.path.join(CODE_ROOT, n, ".git")) for n in names if not n.startswith("."))


def repo_dirs():
    """The units on the map: git repos under the code root, or a monorepo's service folders."""
    try:
        names = sorted(os.listdir(CODE_ROOT))
    except OSError:
        return []
    if not is_monorepo():
        return [n for n in names if os.path.isdir(os.path.join(CODE_ROOT, n, ".git"))]
    return [n for n in names if not n.startswith(".") and n not in SKIP_DIRS and n != "docs"
            and os.path.isdir(os.path.join(CODE_ROOT, n)) and any(os.path.exists(os.path.join(CODE_ROOT, n, f)) for f in UNIT_FILES)]


def _walk(repo_path):
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") or d == ".github"]
        for f in files:
            yield os.path.join(root, f), f


def _deps(repo_path, files):
    """Python, npm, JVM and Go dependency names, each with where it was declared."""
    py, npm, jvm, go = {}, {}, {}, {}
    for path, f in files:
        rel = os.path.relpath(path, repo_path).replace(os.sep, "/")
        if re.match(r"requirements.*\.txt$", f):
            for i, line in enumerate(_read(path).splitlines(), 1):
                m = re.match(r"\s*([A-Za-z0-9_.\-\[\]]+)", line)
                if m and not line.lstrip().startswith(("#", "-")):
                    py.setdefault(re.sub(r"\[.*\]", "", m.group(1)).lower().replace("_", "-"), (rel, i, line.strip()))
        elif f in ("pyproject.toml", "environment.yml", "Pipfile"):
            for i, line in enumerate(_read(path).splitlines(), 1):
                for m in re.finditer(r"(?:^|[\"'\s-])([A-Za-z][A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*(?:[<>=~^!]=?|\s*=\s*[\"'{]|[\"'])", line):
                    name = m.group(1).lower().replace("_", "-")
                    if len(name) > 1 and name not in ("python", "name", "version", "description", "readme", "license", "requires"):
                        py.setdefault(name, (rel, i, line.strip()))
        elif f == "package.json":
            text = _read(path)
            try:
                p = json.loads(text)
            except ValueError:
                continue
            lines = text.splitlines()
            for sect in ("dependencies", "devDependencies", "peerDependencies"):
                for name in p.get(sect, {}) or {}:
                    ln = next((i for i, l in enumerate(lines, 1) if f'"{name}"' in l), 0)
                    npm.setdefault(name, (rel, ln, f'"{name}": "{p[sect][name]}"'))
        elif f == "pom.xml":
            lines = _read(path).splitlines()
            for i, line in enumerate(lines, 1):
                m = re.search(r"<artifactId>\s*([\w.\-]+)\s*</artifactId>", line)
                if m and not any("<plugin>" in l for l in lines[max(0, i - 3):i]):
                    jvm.setdefault(m.group(1), (rel, i, line.strip()))
        elif f in ("build.gradle", "build.gradle.kts"):
            for i, line in enumerate(_read(path).splitlines(), 1):
                for m in re.finditer(r"['\"]([\w.\-]+):([\w.\-]+)(?::[^'\"]*)?['\"]", line):
                    jvm.setdefault(m.group(2), (rel, i, line.strip()))
        elif f == "go.mod":
            # direct requirements only: `// indirect` ones are transitive, replace/exclude blocks are skipped
            block = None
            for i, line in enumerate(_read(path).splitlines(), 1):
                code, _, comment = line.partition("//")
                words = code.split()
                if not words:
                    continue
                if block:
                    if words == [")"]:
                        block = None
                        continue
                    words = [block, *words]
                elif words[-1].endswith("("):
                    block = words[0].rstrip("(")
                    continue
                if words[0] == "require" and len(words) >= 3 and comment.strip() != "indirect":
                    go.setdefault(re.sub(r"/v\d+$", "", words[1]), (rel, i, line.strip()))
    return py, npm, jvm, go


def _spring_props(text, yml):
    """(key, value, line) from a Spring application.yml / .properties; YAML is flattened by indentation."""
    out, stack = [], []
    for i, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not yml:
            m = re.match(r"\s*([\w.\-\[\]]+)\s*[=:]\s*(.*)", raw)
            if m:
                out.append((m.group(1), m.group(2).strip(), i))
            continue
        m = re.match(r"^(\s*)(?:-\s+)?([\w.\-\"'\[\]/*]+)\s*:\s*(.*?)\s*$", raw)
        if not m:
            continue
        indent = len(m.group(1)) + (2 if raw.lstrip().startswith("- ") else 0)
        key, value = m.group(2).strip("\"'"), m.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        full = ".".join([k for _, k in stack] + [key])
        if value and not value.startswith(("|", ">")):
            out.append((full, value.split(" #")[0].strip().strip("\"'"), i))
        else:
            stack.append((indent, key))
    return out


ENV_NAME_RES = [re.compile(r"os\.(?:environ\.get|getenv|environ\[)\(?\s*['\"]([A-Z][A-Z0-9_]+)"),
                re.compile(r"process\.env\.([A-Z][A-Z0-9_]+)"), re.compile(r"process\.env\[['\"]([A-Z][A-Z0-9_]+)"),
                re.compile(r"import\.meta\.env\.([A-Z][A-Z0-9_]+)"), re.compile(r"System\.getenv\(\s*\"([A-Z][A-Z0-9_]+)"),
                re.compile(r"\$\{([A-Z][A-Z0-9_]+)(?::[^}]*)?\}"),
                re.compile(r"^\s{2,}([A-Z][A-Z0-9_]{2,})\s*:\s*(?:str|int|bool|float|Optional|AnyUrl|HttpUrl|PostgresDsn|RedisDsn|SecretStr|list)")]
ENV_HINT = re.compile(r"environ|getenv|process\.env|import\.meta\.env|\$\{[A-Z]|^\s{2,}[A-Z][A-Z0-9_]{2,}\s*:")
CLIENT_RE = re.compile(r"^\s*(?:export\s+)?(?:public\s+|abstract\s+|final\s+)*(?:class|interface)\s+(\w*(?:Client|Api|API|Gateway|Integration|Proxy))\b")
FEIGN_RE = re.compile(r"@FeignClient\s*\((?P<args>[^)]*)\)")
URL_HOST_RE = re.compile(r"\b(?:https?|lb|ws)://([A-Za-z][\w-]*)(?::\d+)?(?=[/\"'\s,;)}]|$)")
# one pass over a line decides whether any detector, client or service-link rule could match it
ANY_CODE = re.compile("|".join(f"(?:{p})" for d in DETECTORS for p in d["code"])
                      + r"|(?:class|interface)\s+\w*(?:Client|Api|API|Gateway|Integration|Proxy)\b|@FeignClient|://|@Enable(?:Zuul|Eureka|Config)", re.M)
SPRING_CONF = re.compile(r"^(?:application|bootstrap)(?:-[\w.-]+)?\.(ya?ml|properties)$")


def scan_repo(name):
    path = os.path.join(CODE_ROOT, name)
    files = list(_walk(path))
    py, npm, jvm, go = _deps(path, files)
    langs, envs, code_hits, entry, deploy, images, clients, props = {}, {}, {}, [], [], {}, {}, []
    links, aliases, role = [], {name}, None  # links: (kind, target name, rel, line, text)
    detectors = [(d, [re.compile(p, re.M) for p in d["code"]]) for d in DETECTORS]
    has_dags = False
    for full, f in files:
        rel = os.path.relpath(full, path).replace(os.sep, "/")
        ext = os.path.splitext(f)[1].lower()
        if f.startswith(".env") and not f.endswith((".example", ".sample")) or "/secrets/" in "/" + rel:
            continue
        if ext in CODE_EXT:
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            lang = CODE_EXT[ext]
            l = langs.setdefault(lang, {"lang": lang, "files": 0, "bytes": 0})
            l["files"] += 1
            l["bytes"] += size
            if "/dags/" in "/" + rel and ext == ".py":
                has_dags = True
            if ext in (".sql", ".sh"):
                continue
            text = _read(full)
            is_test = bool(re.search(r"(^|/)(tests?|__tests__|spec)/|\.(test|spec)\.[jt]sx?$|(^|/)test_[^/]+\.py$|Test\.(java|kt)$|/src/test/", rel))
            for i, line in enumerate(text.splitlines(), 1):
                if len(line) > 400:
                    continue
                if ENV_HINT.search(line):
                    for rx in ENV_NAME_RES:
                        for m in rx.finditer(line):
                            envs.setdefault(m.group(1), (rel, i, line))
                if is_test or not ANY_CODE.search(line) or COMMENT_RE.match(line):
                    continue
                for d, rxs in detectors:
                    if d.get("only") and ext not in d["only"]:  # e.g. jQuery's .delay() isn't Celery
                        continue
                    for rx in rxs:
                        if rx.search(line):
                            hits = code_hits.setdefault(d["id"], [])
                            if len(hits) < 40:
                                hits.append((rel, i, line))
                            break
                m = CLIENT_RE.match(line)
                if m and (" interface " not in f" {line} " or ext in (".java", ".kt")):  # a TS interface is a type, not a client
                    clients.setdefault(m.group(1), (rel, i, line))
                m = FEIGN_RE.search(line)
                if m:
                    args = m.group("args")
                    url = re.search(r"url\s*=\s*\"([^\"]+)\"", args)
                    host = URL_HOST_RE.search(url.group(1)) if url else None
                    nm = re.search(r"(?:name|value|serviceId)\s*=\s*\"([^\"]+)\"", args) or re.match(r"\s*\"([^\"]+)\"", args)
                    target = (host.group(1) if host else None) or (nm.group(1) if nm else None)
                    if target:
                        links.append(("feign", target, rel, i, line))
                    continue
                for h in URL_HOST_RE.findall(line):
                    links.append(("url", h, rel, i, line))
                for rx, label, kind in CODE_ROLE:
                    if re.search(rx, line):
                        role = (label, kind)
                m = re.search(r"new Eureka\(", line)
                if m:
                    aliases.update(re.findall(r"app\s*:\s*['\"]([\w-]+)['\"]", text))
        elif SPRING_CONF.match(f):
            for key, value, i in _spring_props(_read(full), f.endswith(("yml", "yaml"))):
                props.append((key, value, rel, i))
                for e in re.findall(r"\$\{([A-Z][A-Z0-9_]+)", value):
                    envs.setdefault(e, (rel, i, f"{key} = ${{{e}}}"))
                if key == "spring.application.name" and re.match(r"^[\w-]+$", value):
                    aliases.add(value)
                low = key.lower()
                if low.startswith("eureka."):  # registering with the registry is its own link, not an HTTP call
                    continue
                if re.search(r"zuul\.routes\.[\w-]+\.(url|serviceid|service-id)$", low) or re.search(r"gateway\.routes\.(\w+\.)?uri$", low) \
                        or re.search(r"(service|server|client|api)[\w-]*\.(url|uri|host|base-?url)$", low):
                    target = (URL_HOST_RE.search(value) or re.match(r"^([A-Za-z][\w-]*)$", value))
                    if target:
                        links.append(("route" if "routes" in low else "config", target.group(1), rel, i, f"{key}: {value}"))
                elif "://" in value:
                    for h in URL_HOST_RE.findall(value):
                        links.append(("config", h, rel, i, f"{key}: {value}"))
        elif re.match(r"\.?env\.example$|env\.example$|\.env\.sample$|example\.env$", f):
            for i, line in enumerate(_read(full).splitlines(), 1):
                m = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]+)\s*=", line)
                if m:
                    envs.setdefault(m.group(1), (rel, i, m.group(1) + "=…"))
        elif re.match(r"(docker-)?compose.*\.ya?ml$", f) or f.startswith("Dockerfile") or f in ("Makefile", "Procfile") \
                or (ext in (".yml", ".yaml") and re.search(r"k8s|kube|deploy|helm|cloudbuild", rel, re.I)):
            text = _read(full)
            for i, line in enumerate(text.splitlines(), 1):
                m = re.match(r"\s*image:\s*['\"]?([\w./:-]+)", line)
                if m:
                    images.setdefault(m.group(1), (rel, i, line))
                m = re.match(r"\s*-?\s*([A-Z][A-Z0-9_]{2,})\s*[:=]", line)
                if m and ("compose" in f or ext in (".yml", ".yaml")):
                    envs.setdefault(m.group(1), (rel, i, m.group(1)))
                cmd = None
                if f.startswith("Dockerfile"):
                    m = re.match(r"\s*(CMD|ENTRYPOINT)\s+(.+)", line)
                    cmd = m and m.group(2)
                elif "compose" in f:
                    m = re.match(r"\s*command:\s*(.+)", line)
                    cmd = m and m.group(1)
                elif f in ("Makefile", "Procfile"):
                    m = re.search(r"((?:uvicorn|gunicorn|celery|airflow|python -m|node|npm run|java -jar|mvn spring-boot:run)\b.+)", line)
                    cmd = m and m.group(1)
                if cmd:
                    entry.append((rel, i, cmd.strip().strip("[]")))
            if re.search(r"cloudbuild", f):
                deploy.append(("Google Cloud Build", rel))
            elif "compose" in f:
                deploy.append(("Docker Compose", rel))
            elif f.startswith("Dockerfile"):
                deploy.append(("Docker image", rel))
            elif re.search(r"^\s*kind:\s*(Deployment|StatefulSet|Service|CronJob)", text, re.M):
                deploy.append(("Kubernetes", rel))
        elif rel.startswith(".github/workflows/"):
            deploy.append(("GitHub Actions", rel))
        elif f in (".gitlab-ci.yml", "Jenkinsfile"):
            deploy.append(("GitLab CI" if f.startswith(".gitlab") else "Jenkins", rel))
        elif f == "package.json":
            try:
                p = json.loads(_read(full))
            except ValueError:
                p = {}
            if p.get("name") and rel == "package.json":
                aliases.add(str(p["name"]).split("/")[-1])
            scripts = p.get("scripts", {}) or {}
            for k in ("start", "dev", "serve"):
                if k in scripts:
                    entry.append((rel, 0, f"npm run {k}: {scripts[k]}"))
        elif re.match(r"nginx.*\.conf$|.*\.nginx$", f) or (f.endswith(".conf") and "nginx" in rel):
            deploy.append(("nginx", rel))

    # ---- profile ----
    fw = next(((label, kind) for key, label, kind in FRAMEWORKS_NPM if key in npm), None) or \
         next(((label, kind) for key, label, kind in FRAMEWORKS_JVM if key in jvm), None) or \
         next(((label, kind) for key, label, kind in FRAMEWORKS_PY if key in py), None) or \
         next(((label, kind) for key, label, kind in FRAMEWORKS_GO if key in go), None)
    if role:  # @EnableZuulProxy / @EnableEurekaServer say more than the starters
        fw = role
    total = sum(l["bytes"] for l in langs.values()) or 1
    languages = sorted(({**l, "share": round(l["bytes"] / total, 3)} for l in langs.values() if l["lang"] not in ("SQL", "Shell") or len(langs) == 1),
                       key=lambda l: -l["bytes"])
    main_lang = languages[0]["lang"] if languages else None
    prop_lines = [(f"{k}={v}", k, v, rel, i) for k, v, rel, i in props]
    components = []
    for d in DETECTORS:
        ev = []
        dep = next(((n, py[n]) for n in d["py"] if n in py), None) or next(((n, npm[n]) for n in d["npm"] if n in npm), None) \
            or next(((n, jvm[n]) for n in d.get("jvm", []) if n in jvm), None) or next(((n, go[n]) for n in d.get("go", []) if n in go), None)
        if dep:
            ev.append({"type": "dependency", "file": dep[1][0], "line": dep[1][1], "text": mask(dep[1][2])})
        for rel, i, line in code_hits.get(d["id"], [])[:MAX_EVIDENCE]:
            ev.append({"type": "code", "file": rel, "line": i, "text": mask(line)})
        prop_hits = [(k, v, rel, i) for kv, k, v, rel, i in prop_lines if any(re.search(p, kv, re.I) for p in d.get("prop", []))]
        for k, v, rel, i in prop_hits[:3]:
            ev.append({"type": "config", "file": rel, "line": i, "text": mask(f"{k}: {v}")})
        env_hits = [(k, v) for k, v in envs.items() if any(re.search(p, k) for p in d["env"])]
        for k, (rel, i, _) in env_hits[:4]:
            ev.append({"type": "config", "file": rel, "line": i, "text": f"env {k}"})
        img = [(k, v) for k, v in images.items() if any(re.search(p, k, re.I) for p in d["image"])]
        for k, (rel, i, _) in img[:2]:
            ev.append({"type": "config", "file": rel, "line": i, "text": f"image {k}"})
        if not ev:
            continue
        # configuration alone counts only when it's specific: a compose image, a Spring property, or Keycloak's issuer
        kinds = {e["type"] for e in ev}
        if kinds == {"config"} and not img and not prop_hits and not (d["id"] == "keycloak" and env_hits):
            continue
        conf = "wired" if "config" in kinds and ("code" in kinds or "dependency" in kinds) else \
               "used" if "code" in kinds else "declared" if "dependency" in kinds else "configured"
        detail = {}
        if d["id"] == "celery":
            hits = code_hits.get("celery", [])
            detail = {"tasks": sum(1 for h in hits if re.search(r"@(?:shared_task|\w+\.task)\b", h[2])),
                      "producers": sum(1 for h in hits if re.search(r"\.delay\(|\.apply_async\(", h[2])),
                      "worker": any(re.search(r"celery\b.*\bworker", e[2]) for e in entry)}
        components.append({"id": d["id"], "label": d["label"], "kind": d["kind"], "confidence": conf,
                           "evidence": ev, "detail": detail, "env": sorted(k for k, _ in env_hits)})
    kind = fw[1] if fw else None
    comp_ids = {c["id"] for c in components}
    if has_dags or "airflow" in comp_ids and kind is None:
        kind = "pipeline"
    if kind is None:
        if "celery" in comp_ids:
            kind = "worker"
        elif npm and not py and not jvm:
            kind = "service"  # a Node process without a web framework: a consumer, a worker, a socket server
        elif any(d[0] == "Docker image" for d in deploy) and languages:
            kind = "service"
        elif os.path.exists(os.path.join(path, "pyproject.toml")) or os.path.exists(os.path.join(path, "setup.py")):
            kind = "library"
        elif languages:
            kind = "scripts"
        else:
            kind = "config"
    notable = []
    for key, label in NOTABLE.items():
        if (key in py or key in npm or key in jvm) and label not in notable:
            notable.append(label)
    # outbound HTTP by name: client classes and URL-ish env names that aren't infrastructure
    outbound = []
    http_lib = sorted((HTTP_LIBS_PY & set(py)) | (HTTP_LIBS_NPM & set(npm)) | (HTTP_LIBS_JVM & set(jvm)))
    own = _tokens(name) - GENERIC
    for cname, (rel, i, line) in clients.items():
        toks = _tokens(cname) - {"client", "api", "gateway", "integration", "service", "proxy"}
        if not toks or toks & INFRA_WORDS or cname.startswith(("Base", "Abstract", "Mock", "Fake")) or (toks - {t for t in toks if re.fullmatch(r"v\d+", t)}) <= own:
            continue
        outbound.append({"name": cname, "via": "client class", "file": rel, "line": i, "tokens": sorted(toks)})
    for k, (rel, i, _) in envs.items():
        if URL_ENV.search(k) and not INFRA_ENV.search(k) and not any(any(re.search(p, k) for p in d["env"]) for d in DETECTORS):
            outbound.append({"name": k, "via": "env", "file": rel, "line": i, "tokens": sorted(_tokens(k) - GENERIC - {"vite", "react", "next", "public", "url"})})
    seen_e, entrypoints = set(), []
    for rel, i, cmd in entry:
        label = ("Celery worker" if re.search(r"celery\b.*\bworker", cmd) else "Celery beat" if re.search(r"celery\b.*\bbeat", cmd)
                 else "Airflow" if "airflow" in cmd else "API server" if re.search(r"uvicorn|gunicorn|hypercorn|manage\.py runserver|flask run", cmd)
                 else "JVM app" if re.search(r"\bjava\b.*-jar|spring-boot:run", cmd)
                 else "Web dev server" if re.search(r"\bvite\b|next dev|react-scripts start", cmd)
                 else "Node process" if re.search(r"\b(node|nodemon|ts-node|nest start|pm2)\b", cmd) else "Process")
        key = (label, rel)
        if key in seen_e:
            continue
        seen_e.add(key)
        entrypoints.append({"label": label, "command": mask(cmd)[:140], "file": rel, "line": i})
    dep_kinds = []
    for k, rel in deploy:
        if k not in [d["kind"] for d in dep_kinds]:
            dep_kinds.append({"kind": k, "file": rel})
    eureka = next(((n, jvm.get(n) or npm.get(n) or py.get(n)) for n in EUREKA_CLIENT if n in jvm or n in npm or n in py), None)
    return {"id": name, "name": name, "kind": kind, "framework": fw[0] if fw else None, "language": main_lang,
            "languages": languages, "notable": notable, "components": components, "entrypoints": entrypoints[:8],
            "deploy": dep_kinds, "http": http_lib, "outbound": outbound, "env_count": len(envs),
            "files": sum(l["files"] for l in langs.values()), "aliases": sorted(aliases),
            "links": [{"via": v, "target": t, "file": rel, "line": i, "text": mask(txt)} for v, t, rel, i, txt in links],
            "registry_client": {"file": eureka[1][0], "line": eureka[1][1], "text": mask(eureka[1][2])} if eureka else None}


def _frontend_links(repos):
    """Frontend → backend calls matched by URL, from teammap's index (exact, with counts)."""
    try:
        import io
        import contextlib
        from . import teammap
        with contextlib.redirect_stdout(io.StringIO()):
            loaded = [teammap.load_repo(n) for n in teammap.REPOS if os.path.isdir(os.path.join(CODE_ROOT, n))]
            L = teammap.link(loaded)
    except Exception:
        return []
    counts = {}
    for a in L.get("apifns", []):
        ep = a.get("endpoint")
        if not ep:
            continue
        key = (a["repo"], ep["repo"])
        c = counts.setdefault(key, {"n": 0, "example": None})
        c["n"] += 1
        c["example"] = c["example"] or {"file": a.get("file"), "line": a.get("line"), "text": f"{ep.get('method', '').upper()} {ep.get('path', '')}"}
    return [{"from": f, "to": t, "kind": "http", "label": f"HTTP · {c['n']} API calls", "confidence": "exact",
             "evidence": [{"type": "code", "file": c["example"]["file"], "line": c["example"]["line"] or 0, "text": c["example"]["text"]}] if c["example"] else []}
            for (f, t), c in counts.items() if f in repos and t in repos and f != t]


def _match_repo(tokens, repos, self_id):
    """The repo an outbound name points at: all of the repo's distinctive words appear in the name."""
    best, best_score = None, 0
    tok = set(tokens)
    ver = next((t for t in tok if re.fullmatch(r"v\d+", t)), None)
    for r in repos:
        if r == self_id:
            continue
        rt = _tokens(r) - GENERIC
        rver = next((t for t in rt if re.fullmatch(r"v\d+", t)), None)
        words = rt - ({rver} if rver else set())
        if not words or not words <= tok:
            continue
        if ver and rver and ver != rver or ver and ver != "v1" and not rver or rver and not ver:
            continue
        score = len(words) + (1 if ver and ver == rver else 0)
        if score > best_score:
            best, best_score = r, score
    return best


def build():
    t0 = time.time()
    names = repo_dirs()
    repos = [scan_repo(n) for n in names]
    ids = [r["id"] for r in repos]
    by_alias = {}
    for r in repos:
        for a in r["aliases"]:
            by_alias.setdefault(_key(a), r["id"])
    registry = next((r["id"] for r in repos if r["kind"] == "registry" and (r["framework"] or "").startswith("Eureka")), None)
    edges, nodes = [], {}
    for r in repos:
        for c in r["components"]:
            nid = "c:" + c["id"]
            nodes.setdefault(nid, {"id": nid, "label": c["label"], "kind": c["kind"], "repos": []})["repos"].append(r["id"])
            verb = {"database": "SQL" if c["id"] in ("postgres", "mysql", "sqlite") else "data", "cache": "cache",
                    "broker": {"rabbitmq": "AMQP", "mqtt": "MQTT", "kafka": "Kafka"}.get(c["id"], "events"), "worker": "jobs",
                    "auth": "auth", "storage": "files", "observability": "metrics" if c["id"] == "prometheus" else "errors",
                    "search": "search", "external": ""}.get(c["kind"], "")
            edges.append({"from": r["id"], "to": nid, "kind": c["kind"], "label": verb, "confidence": c["confidence"], "evidence": c["evidence"][:4]})
        # service-to-service links that name the service: Feign clients, gateway routes, URLs with a service host
        exact = {}
        for l in r["links"]:
            to = by_alias.get(_key(l["target"]))
            if not to or to == r["id"]:
                if l["via"] in ("feign", "route") and l["target"].lower() not in NOT_HOSTS and not to:
                    xid = "x:" + l["target"].lower()
                    nodes.setdefault(xid, {"id": xid, "label": l["target"], "kind": "external", "repos": []})["repos"].append(r["id"])
                    exact.setdefault(xid, []).append(l)
                continue
            exact.setdefault(to, []).append(l)
        for to, ls in exact.items():
            vias = sorted({l["via"] for l in ls})
            label = {"feign": "Feign", "route": "route", "config": "HTTP", "url": "HTTP"}
            edges.append({"from": r["id"], "to": to, "kind": "http", "label": " · ".join(label[v] for v in vias) + (f" · {len(ls)}" if len(ls) > 1 else ""),
                          "confidence": "exact", "evidence": [{"type": "code" if l["via"] in ("feign", "url") else "config", "file": l["file"], "line": l["line"], "text": l["text"]} for l in ls[:4]]})
        if r["registry_client"] and registry and registry != r["id"]:
            edges.append({"from": r["id"], "to": registry, "kind": "discovery", "label": "registers", "confidence": "declared",
                          "evidence": [{"type": "dependency", **r["registry_client"]}]})
        targets = {}
        for o in r["outbound"]:
            to = _match_repo(o["tokens"], ids, r["id"])
            o["target"] = to
            if to and to not in exact:
                t = targets.setdefault(to, {"names": [], "evidence": []})
                t["names"].append(o["name"])
                t["evidence"].append({"type": "code" if o["via"] == "client class" else "config", "file": o["file"], "line": o["line"], "text": o["name"]})
            elif not to and o["via"] == "client class" and o["tokens"] and r["kind"] != "frontend":  # a frontend's API wrappers aren't outside services
                xid = "x:" + "-".join(o["tokens"])
                nodes.setdefault(xid, {"id": xid, "label": " ".join(o["tokens"]).title() + " service", "kind": "external", "repos": []})["repos"].append(r["id"])
                edges.append({"from": r["id"], "to": xid, "kind": "http", "label": "HTTP", "confidence": "inferred",
                              "evidence": [{"type": "code", "file": o["file"], "line": o["line"], "text": o["name"]}]})
        for to, t in targets.items():
            edges.append({"from": r["id"], "to": to, "kind": "http", "label": "HTTP · " + ", ".join(t["names"][:2]),
                          "confidence": "inferred", "evidence": t["evidence"][:4]})
    exact_fe = _frontend_links(set(ids))
    for e in exact_fe:  # an exact URL match replaces any other link between the same two repos
        edges = [x for x in edges if not (x["from"] == e["from"] and x["to"] == e["to"])]
    edges += exact_fe
    for n in nodes.values():
        n["repos"] = sorted(set(n["repos"]))
    return {"generated": time.time(), "seconds": round(time.time() - t0, 2), "root": norm(CODE_ROOT), "monorepo": is_monorepo(),
            "repos": repos, "nodes": list(nodes.values()), "edges": edges}


_cache = {"at": 0, "data": None}
CACHE_FILE = os.path.join(GRAPHS, "index", "arch.json")  # not graph/*.json: the Brain merges those as code graphs


def fingerprint():
    """Changes when any scanned file (or this code) changes; walking is fast, reading is what's slow."""
    import hashlib
    h = hashlib.sha1()
    for mod in ("archmap.py", "teammap.py"):
        try:
            h.update(str(os.path.getmtime(os.path.join(HERE, mod))).encode())
        except OSError:
            pass  # an installed package may not ship the module; its absence is part of the fingerprint
    for name in repo_dirs():
        for full, _ in _walk(os.path.join(CODE_ROOT, name)):
            try:
                st = os.stat(full)
            except OSError:
                continue
            h.update(f"{full}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    return h.hexdigest()


def cached(force=False):
    """The map from <team>/graph/arch.json when nothing changed since it was built, else a fresh scan."""
    fp = fingerprint()
    if not force:
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("fingerprint") == fp:
                return data
        except (OSError, ValueError):
            pass  # no usable cache: build the map again
    data = build()
    data["fingerprint"] = fp
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass  # a read-only workspace still gets the map, just without the cache
    return data


def get(max_age=60):
    if not _cache["data"] or time.time() - _cache["at"] > max_age:
        _cache["data"], _cache["at"] = cached(force=max_age == 0), time.time()
    return _cache["data"]


# ---- queries (for agents: `teammap arch …`) ---------------------------------------------------------

def resolve(data, q):
    """A repo/service or a component by id, name, alias or a unique part of one: ("repo"|"node", item) or (None, candidates)."""
    k = _key(q)
    for r in data["repos"]:
        if _key(r["id"]) == k or any(_key(a) == k for a in r.get("aliases", [])):
            return "repo", r
    for n in data["nodes"]:
        if _key(n["id"].split(":", 1)[1]) == k or _key(n["label"]) == k:
            return "node", n
    reps = [r for r in data["repos"] if k in _key(r["id"])]
    nds = [n for n in data["nodes"] if k in _key(n["label"]) or k in _key(n["id"])]
    if len(reps) + len(nds) == 1:
        return ("repo", reps[0]) if reps else ("node", nds[0])
    return None, [r["id"] for r in reps] + [n["label"] for n in nds]


def _calls(data):
    """Service-to-service links only (no components, no registry)."""
    return [e for e in data["edges"] if e["kind"] == "http" and not e["to"].startswith("c:")]


def impact(data, q, depth=3):
    kind, item = resolve(data, q)
    if kind != "repo":
        return None, item
    levels, seen, frontier = [], {item["id"]}, [item["id"]]
    for _ in range(depth):
        nxt = []
        for e in _calls(data):
            if e["to"] in frontier and e["from"] not in seen:
                seen.add(e["from"])
                nxt.append(e)
        if not nxt:
            break
        levels.append(nxt)
        frontier = [e["from"] for e in nxt]
    return item, levels


def path(data, a, b):
    ka, ra = resolve(data, a)
    kb, rb = resolve(data, b)
    if not ka or not kb:
        return None, (ra if not ka else rb)
    src, dst = (ra["id"] if ka == "repo" else ra["id"]), (rb["id"] if kb == "repo" else rb["id"])
    edges = _calls(data) + [e for e in data["edges"] if e["to"] == dst]  # a component can be the destination
    prev, q = {src: None}, [src]
    while q:
        cur = q.pop(0)
        if cur == dst:
            break
        for e in edges:
            if e["from"] == cur and e["to"] not in prev:
                prev[e["to"]] = e
                q.append(e["to"])
    if dst not in prev:
        return (src, dst), None
    hops, cur = [], dst
    while prev[cur]:
        hops.append(prev[cur])
        cur = prev[cur]["from"]
    return (src, dst), list(reversed(hops))


def _where(e):
    ev = e["evidence"][0] if e.get("evidence") else None
    return f"   {e['from']}/{ev['file']}:{ev['line']}" if ev else ""


def _name(data, nid):
    n = next((n for n in data["nodes"] if n["id"] == nid), None)
    return n["label"] if n else nid


def node_text(data, n):
    users = [e for e in data["edges"] if e["to"] == n["id"]]
    lines = [f"{n['label']}  [{n['kind']}]  used by {len(users)}"]
    for e in sorted(users, key=lambda e: e["from"]):
        r = next((x for x in data["repos"] if x["id"] == e["from"]), None)
        c = next((c for c in (r or {}).get("components", []) if "c:" + c["id"] == n["id"]), None)
        env = f" · env {', '.join(c['env'][:3])}" if c and c["env"] else ""
        ev = next((x for x in (c or e)["evidence"] if x["type"] == "code"), None) or ((c or e)["evidence"] or [None])[0]
        where = f"   {e['from']}/{ev['file']}:{ev['line']}" if ev else ""
        lines.append(f"  {e['from']:32} {e['confidence']}{env}{where}")
    return "\n".join(lines)


# ---- text and Mermaid ------------------------------------------------------------------------------

MAIN_KINDS = ("frontend", "gateway", "api", "service", "worker", "pipeline", "registry")


def mermaid(data, include_minor=False):
    main = [r for r in data["repos"] if include_minor or r["kind"] in MAIN_KINDS]
    ids = {r["id"] for r in main}
    safe = lambda s: re.sub(r"[^A-Za-z0-9_]", "_", s)
    out = ["flowchart LR"]
    for r in main:
        sub = " · ".join(x for x in (r["framework"], r["language"]) if x)
        out.append(f'  {safe(r["id"])}["{r["name"]}<br/><small>{sub}</small>"]')
    used = {e["to"] for e in data["edges"] if e["from"] in ids and e["to"].startswith(("c:", "x:"))}
    shapes = {"database": '[("{}")]', "cache": '[("{}")]', "broker": '{{"{}"}}', "worker": '[["{}"]]', "auth": '(["{}"])'}
    for n in data["nodes"]:
        if n["id"] in used:
            out.append(f'  {safe(n["id"])}' + shapes.get(n["kind"], '("{}")').format(n["label"]))
    for e in data["edges"]:
        if e["from"] in ids and (e["to"] in ids or e["to"] in used) and e["kind"] != "discovery":
            arrow = "-.->" if e["confidence"] in ("inferred", "declared") else "-->"
            label = f'|{e["label"]}|' if e["label"] else ""
            out.append(f'  {safe(e["from"])} {arrow}{label} {safe(e["to"])}')
    return "\n".join(out)


def text(data, repo=None):
    if repo:
        kind, item = resolve(data, repo)
        if kind == "node":
            return node_text(data, item)
        if kind == "repo":
            repo = item["id"]
        elif item:
            return f"{repo!r} matches several: " + ", ".join(item[:20]) + " — be more specific"
    lines = []
    repos = [r for r in data["repos"] if not repo or r["id"] == repo]
    if not repos:
        return f"no repository matches {repo!r}; repos: " + ", ".join(r["id"] for r in data["repos"])
    detail = bool(repo) or len(repos) == 1
    for r in repos:
        langs = ", ".join(f"{l['lang']} {round(l['share'] * 100)}%" for l in r["languages"][:3])
        lines.append(f"{r['id']}  [{r['kind']}]  {r['framework'] or '-'}  ({langs or 'no code'})")
        if detail:
            if r["notable"]:
                lines.append("  stack     " + ", ".join(r["notable"]))
            for e in r["entrypoints"]:
                lines.append(f"  runs      {e['label']}: {e['command']}   ({e['file']})")
            if r["deploy"]:
                lines.append("  deploy    " + ", ".join(d["kind"] for d in r["deploy"]))
        for c in r["components"]:
            ev = next((e for e in c["evidence"] if e["type"] == "code"), None) or next((e for e in c["evidence"] if e["type"] == "config"), c["evidence"][0])
            extra = f" · {c['detail']['tasks']} tasks, {c['detail']['producers']} enqueue sites" if c["detail"] else ""
            lines.append(f"  uses      {c['label']} ({c['confidence']}{extra})   {ev['file']}:{ev['line']}")
        for e in data["edges"]:
            if e["from"] == r["id"] and not e["to"].startswith("c:") and (detail or e["kind"] != "discovery"):
                ev = e["evidence"][0] if e["evidence"] else None
                where = f"   {ev['file']}:{ev['line']}" if ev and detail else ""
                lines.append(f"  calls     {e['to'].removeprefix('x:')} · {e['label']} ({e['confidence']}){where}")
            if e["to"] == r["id"] and (detail or e["kind"] != "discovery"):
                lines.append(f"  called by {e['from']} · {e['label']} ({e['confidence']})")
        lines.append("")
    return "\n".join(lines).rstrip()


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    data = cached(force="--rescan" in argv)
    as_json = "--json" in argv
    rest = [a for a in argv if not a.startswith("--")]
    if "--mermaid" in argv:
        print(mermaid(data, "--all" in argv))
    elif rest[:1] == ["impact"] and len(rest) > 1:
        item, levels = impact(data, rest[1])
        if item is None:
            print(f"no service matches {rest[1]!r}" + (": " + ", ".join(levels[:20]) if levels else ""))
        elif as_json:
            print(json.dumps({"service": item["id"], "levels": levels}, indent=1))
        else:
            print(f"{item['id']}: who calls it" + ("" if levels else " — nothing in the workspace calls it over HTTP"))
            for i, lv in enumerate(levels, 1):
                for e in lv:
                    print(f"  {'  ' * (i - 1)}{'←' if i == 1 else '↖'} {e['from']} → {e['to']} · {e['label']} ({e['confidence']}){_where(e)}")
    elif rest[:1] == ["path"] and len(rest) > 2:
        ends, hops = path(data, rest[1], rest[2])
        if ends is None:
            print(f"no match for one of {rest[1]!r}, {rest[2]!r}" + (": " + ", ".join(hops[:20]) if hops else ""))
        elif hops is None:
            back = path(data, rest[2], rest[1])[1]
            print(f"no call path {_name(data, ends[0])} → {_name(data, ends[1])}" + (f"; the other way: {' → '.join([back[0]['from']] + [h['to'] for h in back])}" if back else ""))
        elif as_json:
            print(json.dumps(hops, indent=1))
        else:
            print(" → ".join([hops[0]["from"]] + [_name(data, h["to"]) for h in hops]) if hops else ends[0])
            for h in hops:
                print(f"  {h['from']} → {_name(data, h['to'])} · {h['label']} ({h['confidence']}){_where(h)}")
    elif as_json:
        if rest:
            kind, item = resolve(data, rest[0])
            edges = [e for e in data["edges"] if item and kind and (e["from"] == item["id"] or e["to"] == item["id"])]
            print(json.dumps({"kind": kind, "item": item, "edges": edges}, indent=1))
        else:
            print(json.dumps(data, indent=1))
    else:
        print(text(data, rest[0] if rest else None))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
