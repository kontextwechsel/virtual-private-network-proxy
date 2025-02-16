ARG VERSION=latest

FROM alpine:${VERSION}

RUN apk update
RUN apk add openconnect python3 py3-yaml

ADD proxy.py /proxy/
ADD proxy.yaml /proxy/

ENV PYTHONUNBUFFERED=1

CMD ["/proxy/proxy.py", "-c", "/proxy/proxy.yaml"]
