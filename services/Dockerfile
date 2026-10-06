# One Dockerfile for every service; build arg SERVICE selects ./<service>.
# Build context = the target repo root (so pkg/obs is available to all services).
# VERSION comes from <service>/VERSION, COMMIT from the target repo HEAD (build arg).
FROM golang:1.26-alpine AS build
ARG SERVICE
ARG COMMIT=unknown
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    VERSION=$(cat ${SERVICE}/VERSION) && \
    go build -ldflags "-s -w -X main.version=${VERSION} -X main.commit=${COMMIT}" -o /out/app ./${SERVICE}

FROM alpine:3.22
ARG SERVICE
WORKDIR /app
COPY --from=build /out/app /app/app
COPY ${SERVICE}/config.yaml /app/config.yaml
USER nobody
ENTRYPOINT ["/app/app"]
