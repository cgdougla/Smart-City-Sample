#!/usr/bin/python3

from db_ingest import DBIngest
from db_query import DBQuery
from signal import signal, SIGTERM
from subprocess import Popen

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import paho.mqtt.client as mqtt
import json
import requests

from utils import logging
from threading import Thread
from threading import Timer
from probe import probe, run
import time
import os
import tempfile
import shutil
import re

logger = logging.get_logger('main', is_static=True)

class Feeder():

    def __init__(self):
        logger.debug("Initializing Feeder")

        self.office = list(map(float, os.environ["OFFICE"].split(",")))
        self.alg_id = None
        self.recording_volume = os.environ["STORAGE_VOLUME"]
        self.every_nth_frame = int(os.environ["EVERY_NTH_FRAME"])

        #Hosts
        self.dbhost = os.environ["DBHOST"]
        self.vahost = "http://localhost:8080/pipelines"
        self.mqtthost = os.environ["MQTTHOST"]

        #Clients
        self.db_alg = DBIngest(host=self.dbhost, index="algorithms", office=self.office)
        self.db_inf = DBIngest(host=self.dbhost, index="analytics", office=self.office)
        self.db_sensors = DBQuery(host=self.dbhost, index="sensors", office=self.office)
        self.mqttclient = None
        self.mqtttopic = None
        self.observer = Observer()

        self.batchsize = 300
        self.inference_cache = []

        self._threadflag = False

    def start(self):

        logger.info(" ### Starting Feeder ### ")

        logger.debug("Waiting for VA startup")
        r = requests.Response()
        r.status_code=400
        while r.status_code != 200 and r.status_code != 201:
            try:
                r = requests.get(self.vahost)
            except Exception as e:
                r = requests.Response()
                r.status_code=400

            time.sleep(10)
        
        # Register Algorithm
        logger.debug("Registering as algorithm in the DB")

        while True:
            try:
                self.alg_id = self.db_alg.ingest({
                    "name": "object_detection",
                    "office": {
                        "lat": self.office[0],
                        "lon": self.office[1]
                    },
                    "status": "idle",
                    "skip": self.every_nth_frame,
                })["_id"]
                break
            except Exception as e:
                logger.debug("Register algo exception: "+str(e))
                time.sleep(10)

        self.mqtttopic = "smtc_va_inferences_" + self.alg_id

        camera_monitor_thread = Thread(target=self.monitor_cameras, daemon=True)
        
        logger.debug("Starting working threads")
        self._threadflag = True
        self.startmqtt()
        self.observer.start()
        camera_monitor_thread.start()

        logger.debug("Waiting for interrupt...")
        camera_monitor_thread.join()
        self.observer.join()

    def stop(self):
        logger.info(" ### Stopping Feeder ### ")

        self._threadflag = False

        logger.debug("Unregistering algorithm from DB")
        self.db_alg.delete(self.alg_id)

        self.mqttclient.loop_stop()
        self.observer.stop()

    def startmqtt(self):
        self.mqttclient = mqtt.Client("feeder_" + self.alg_id)
        self.mqttclient.connect(self.mqtthost)
        self.mqttclient.on_message = self.mqtt_handler
        self.mqttclient.loop_start()
        self.mqttclient.subscribe(self.mqtttopic)
    
    def mqtt_handler(self, client, userdata, message):
        m_in = json.loads(str(message.payload.decode("utf-8", "ignore")))

        for tag in m_in["tags"]:
            m_in[tag] = m_in["tags"][tag]

        del m_in["tags"]
        
        m_in["time"] = m_in["real_base"] + m_in["timestamp"]
        # convert to milliseconds
        m_in["time"] = int(m_in["time"]/1000000)
        self.inference_cache.append(m_in)
        if len(self.inference_cache) >= self.batchsize:
            try:
                self.db_inf.ingest_bulk(self.inference_cache[:self.batchsize])
                self.inference_cache = self.inference_cache[self.batchsize:]
            except Exception as e:
                logger.debug("Ingest Error: " + str(e))

    def monitor_cameras(self):
        logger.debug("Starting Sensor Monitor Thread")
        while self._threadflag:
            logger.debug("Searching for sensors...")

            try:
                for sensor in self.db_sensors.search("sensor:'camera' and status:'idle' and office:[" + str(self.office[0]) + "," + str(self.office[1]) + "]"):
                    logger.debug(sensor)
                    try:
                        fswatch = None
                        logger.debug("Sensor found! " + sensor["_id"])
                        logger.debug("Setting sensor " + sensor["_id"] + " to streaming")
                        r = self.db_sensors.update(sensor["_id"], {"status": "streaming"}, version=sensor["_version"])

                        logger.debug("Setting algorithm to streaming from sensor " + sensor["_id"])
                        r = self.db_alg.update(self.alg_id, {
                            "source": sensor["_id"],
                            "status": "processing"
                        })

                        # Attempt to POST to VA service
                        jsonData = {
                            "source": {
                                "uri": sensor["_source"]["url"],
                                "type":"uri"
                            },
                            "tags": {
                                "algorithm": self.alg_id,
                                "sensor": sensor["_id"],
                                "office": {
                                    "lat": self.office[0],
                                    "lon": self.office[1],
                                },
                            },
                            "parameters": {
                                "every-nth-frame": self.every_nth_frame,
                                "recording_prefix": "recordings/" + sensor["_id"],
                                "method": "mqtt",
                                "address": self.mqtthost,
                                "clientid": self.alg_id,
                                "topic": self.mqtttopic
                            },
                        }

                        folderpath = os.path.join(os.path.realpath(self.recording_volume), sensor["_id"])
                        if not os.path.exists(folderpath):
                            os.makedirs(folderpath)

                        logger.debug("Adding folder watch for " + folderpath)
                        filehandler = FSHandler(sensor=sensor["_id"], office=self.office, dbhost=self.dbhost, rec_volume=self.recording_volume)
                        fswatch = self.observer.schedule(filehandler, folderpath, recursive=True)

                        try:
                            logger.info("Posting Request to VA Service")
                            r = requests.post(self.vahost + "/object_detection/2", json=jsonData, timeout=10)
                            r.raise_for_status()
                            pipeline_id = None

                            if r.status_code == 200:
                                logger.debug("Started pipeline " + r.text)
                                pipeline_id = int(r.text)

                            while r.status_code == 200:
                                logger.debug("Querying status of pipeline")
                                r = requests.get(self.vahost + "/object_detection/2/" + str(pipeline_id) + "/status", timeout=10)
                                r.raise_for_status()
                                jsonValue = r.json()
                                if "avg_pipeline_latency" not in jsonValue:
                                    jsonValue["avg_pipeline_latency"] = 0
                                state = jsonValue["state"]
                                try:
                                    logger.debug("fps: ")
                                    logger.debug(str(jsonValue))
                                except:
                                    logger.debug("error")
                                logger.debug("Pipeline state is " + str(state))
                                if state == "COMPLETED" or state == "ABORTED" or state == "ERROR":
                                    logger.debug("Pipeline ended")
                                    break

                                self.db_alg.update(self.alg_id, {"performance": jsonValue["avg_fps"], "latency": jsonValue["avg_pipeline_latency"]*1000})

                                time.sleep(10)

                            logger.debug("Setting sensor " + sensor["_id"] + " to disconnected")
                            r = self.db_sensors.update(sensor["_id"], {"status": "disconnected"})

                        except requests.exceptions.RequestException as e:
                            logger.error("Feeder: Request to VA Service Failed: " + str(e))
                            logger.debug("Setting sensor " + sensor["_id"] + " to idle")
                            r = self.db_sensors.update(sensor["_id"], {"status": "idle"})

                    except Exception as e:
                        logger.error("Feeder Exception: " + str(e))

                    if fswatch:
                        self.observer.unschedule(fswatch)
                        del(filehandler)

                    logger.debug("Setting algorithm to idle")
                    r = self.db_alg.update(self.alg_id, {"status": "idle"})
                    break
            except Exception as e:
                print(e, flush=True)

            time.sleep(5)
        
        logger.debug("Sensor monitor thread done")

class FSHandler(FileSystemEventHandler):
    def __init__(self, sensor, office, dbhost, rec_volume):
        self.sensor = sensor
        self.office = office
        self.db_rec = DBIngest(host=dbhost, index="recordings", office=office)
        self.db_sensors = DBQuery(host=dbhost, index="sensors", office=office)
        self.recording_volume = rec_volume

        self.last_file = None
        self.finalize_timer = None
        self.record_cache = []
        self.timeout = 80 #set to 20 seconds... this should change according to recording chunk length
    
    def on_created(self, event):
        if event.is_directory: return
        if event.src_path.endswith(".png"): return
        if self.last_file and (self.last_file==event.src_path): return
        if self.finalize_timer: self.finalize_timer.cancel()
        if self.last_file:
            try:
                self.ingest()
            except Exception as error:
                logger.error("Failed to ingest: %s %s\n" %(self.last_file,error))

        logger.debug("Started recording new file! " + event.src_path)
        self.last_file = event.src_path

        del(self.finalize_timer)
        self.finalize_timer = Timer(self.timeout, self.ingest)
        self.finalize_timer.start()
        logger.debug("Started file watch timer for " + str(self.timeout) + " seconds")
                    
    def ffmpeg_convert(self,filename):        
        with tempfile.TemporaryDirectory() as tmpdirname:
            filename = os.path.abspath(filename)
            tmpfilename = os.path.abspath(os.path.join(tmpdirname,os.path.basename(filename)))
            output=""
            try:
                list(run(["/usr/bin/ffmpeg","-i",filename,"-c","copy",tmpfilename]))
                shutil.move(tmpfilename,filename)
                list(run(["/usr/bin/ffmpeg","-i",filename,"-vf","thumbnail,scale=640:360","-frames:v","1",filename+".png"]))
                return filename,probe(filename)
            except Exception as error:
                logger.error("Error converting mp4 with ffmpeg: %s %s" %(error,error.output))
                raise

    def get_timestamp(self,filename):
        parsed = os.path.basename(filename).split('_')
        return int(int(parsed[-2])/1000000)

    def ingest(self):
        logger.debug("Finished recording file " + self.last_file)
        converted_file,sinfo=self.ffmpeg_convert(self.last_file)
        sinfo.update({
            "sensor": self.sensor,
            "office": {
                "lat": self.office[0],
                "lon": self.office[1],
            },
            "time": self.get_timestamp(converted_file),
            "path": os.path.abspath(converted_file).split(os.path.abspath(self.recording_volume)+"/")[1],
        })
        
        # calculate total bandwidth
        bandwidth=0
        for stream1 in sinfo["streams"]:
            if "bit_rate" in stream1: 
                bandwidth=bandwidth+stream1["bit_rate"]
        self.db_sensors.update(self.sensor, {"bandwidth": bandwidth})
        self.db_rec.ingest(sinfo)
        self.record_cache.append(sinfo)

if __name__ == '__main__':
    smtc_feeder = Feeder()

    def quit_service(signum, sigframe):
        try:
            smtc_feeder.stop()
        except Exception as e:
            pass
        exit(143)
    signal(SIGTERM, quit_service)

    try:
        smtc_feeder.start()
    except KeyboardInterrupt:
        smtc_feeder.stop()
