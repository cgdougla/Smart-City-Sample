# smtc_video_analytics

ARG base_name=video_analytics_service_gstreamer

FROM ${base_name}
COPY requirements.txt /home/
RUN python3 -m pip install -r /home/requirements.txt
USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
    ffmpeg
RUN rm -r /home/video-analytics/models && rm -r /home/video-analytics/pipelines
COPY *.py /home/
COPY ./models /home/video-analytics/models
COPY ./pipelines/gstreamer /home/video-analytics/pipelines
COPY ./utils /home/utils
COPY start-service.sh /home/

WORKDIR /home/
ENTRYPOINT ["/home/start-service.sh"]

####
ARG  USER
ARG  GROUP
ARG  UID
ARG  GID
## must use ; here to ignore user exist status code
RUN  groupadd -f -g ${GID} ${GROUP};useradd -d /home/${USER} -g ${GROUP} -K UID_MAX=${UID} -K UID_MIN=${UID} ${USER};echo
USER ${USER}
####
