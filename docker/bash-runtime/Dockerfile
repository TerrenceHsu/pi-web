# Build explicitly with an approved immutable Python slim base reference.
# No default tag and no package download during the build.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN mkdir -p /opt/pi-agent-runtime /workspace /run/pi-command \
    && test -x /bin/bash && command -v python3
COPY src/coding_sandbox/local_worker.py /opt/pi-agent-runtime/worker.py
RUN chmod 0555 /opt/pi-agent-runtime /opt/pi-agent-runtime/worker.py
LABEL io.pi-agent.bash-runtime="1"
LABEL io.pi-agent.bash-script="1"
# The inert PID 1 has a different unprivileged UID from user commands. User
# code must not be able to signal/ptrace the supervisor and survive quiescence.
USER 10000:10000
WORKDIR /workspace
ENTRYPOINT ["python3", "-I", "-S", "-B", "/opt/pi-agent-runtime/worker.py", "idle"]
CMD ["1800"]
