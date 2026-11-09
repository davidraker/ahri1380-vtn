# AHRI 1380 VTN test tool: pinned runtime so the OpenSSL build and its cipher policy are known.
# Debian 12 (bookworm) ships OpenSSL 3.0 LTS without a distribution crypto-policy layer, so both
# OpenADR 2.0b default cipher suites are available; the build fails if the self-check disagrees.
#
#   docker build -t ahri-vtn .
#   docker run --rm -p 443:443 -p 9443:9443 -v $PWD/certs:/data/certs -v $PWD/config.toml:/data/config.toml \
#              -v ahri-vtn-db:/data -e AHRI_VTN_DB_PASSWORD=... ahri-vtn
# SQLite lives in the /data volume; for PostgreSQL set db_url in config.toml and pass the password
# through the environment variable named by db_password_env.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 1000 ahri

WORKDIR /app
COPY openleadr-python /app/openleadr-python
COPY pyproject.toml README.md /app/
COPY ahri_vtn /app/ahri_vtn
COPY sim_ven /app/sim_ven
RUN pip install ./openleadr-python && pip install ".[postgres]" \
    && python -c "import ssl; print(ssl.OPENSSL_VERSION)"

# Fail the build if this runtime cannot negotiate the OpenADR 2.0b default cipher suites.
RUN mkdir -p /tmp/selfcheck && cd /tmp/selfcheck \
    && ahri-vtn certs quickstart --san localhost > /dev/null \
    && printf '[tls]\ncert="certs/vtn-rsa.crt"\nkey="certs/vtn-rsa.key"\necc_cert="certs/vtn-ecc.crt"\necc_key="certs/vtn-ecc.key"\nclient_ca_file="certs/ven-trust.pem"\n[ui]\noperator_ca_file="certs/operator-ca.crt"\nbasic_auth_user="x"\nbasic_auth_password_hash="pbkdf2_sha256$1$YQ==$YQ=="\n' > config.toml \
    && ahri-vtn tls-selfcheck --config config.toml && cd / && rm -rf /tmp/selfcheck

USER ahri
WORKDIR /data
VOLUME ["/data"]
EXPOSE 443 9443
# The UI must listen on all interfaces inside the container; publish 9443 only where the operator is.
ENTRYPOINT ["ahri-vtn"]
CMD ["run", "--config", "/data/config.toml"]
