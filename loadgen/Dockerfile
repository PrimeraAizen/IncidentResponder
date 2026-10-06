# Load generator image (harness, not part of the system under investigation).
FROM golang:1.26-alpine AS build
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN go build -ldflags "-s -w" -o /out/loadgen .

FROM alpine:3.22
COPY --from=build /out/loadgen /usr/local/bin/loadgen
USER nobody
ENTRYPOINT ["loadgen"]
