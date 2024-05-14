import os
import shlex
import tempfile
import threading
from dataclasses import asdict
from subprocess import run
from threading import Lock
from typing import List, Optional

import requests
from flask import Flask, request

import jasnah
from jasnah.config import CONFIG, DATA_FOLDER
from jasnah.db import Experiment, Supervisor, db

app = Flask(__name__)

REPOSITORIES = DATA_FOLDER / "repositories"
LOCK = Lock()
EXPERIMENT: Optional[Experiment] = None
SUPERVISOR_ID = CONFIG.supervisor_id
SERVER_URL = CONFIG.server_url


@app.route("/status")
def status():
    return {"status": "ok", "experiment": EXPERIMENT, "id": SUPERVISOR_ID}


def repository_name(repository: str):
    return repository.split("/")[-1]


def run_experiment_inner(experiment: Experiment, supervisors: List[Supervisor]):
    name = repository_name(experiment.repository)
    repository_path = REPOSITORIES / name

    if not repository_path.exists():
        run(["git", "clone", experiment.repository, repository_path])

    run(["git", "pull"], cwd=repository_path)
    run(["git", "reset", "--hard"], cwd=repository_path)
    run(["git", "checkout", experiment.commit], cwd=repository_path)

    if experiment.diff:
        # TODO: Test this is working as expected
        with tempfile.NamedTemporaryFile("w") as f:
            f.write(experiment.diff)
            run(["git", "apply", f.name], cwd=repository_path)

    assigned_supervisors = ",".join(s.id for s in supervisors)
    env = os.environ.copy()
    env["ASSIGNED_SUPERVISORS"] = assigned_supervisors

    command = shlex.split(experiment.command)
    run(command, cwd=repository_path, env=env)


def run_experiment(experiment: Experiment):
    supervisors = db.get_assigned_supervisors(experiment.id)

    if SUPERVISOR_ID == supervisors[0].id:
        db.set_experiment_status(experiment.id, "running")

    jasnah.log(
        {
            "info": "start experiment",
            "id": experiment.id,
            "name": experiment.name,
            "author": experiment.author,
        }
    )

    run_experiment_inner(experiment, supervisors)

    db.set_experiment_status(experiment.id, "done")
    db.set_supervisor_status(SUPERVISOR_ID, "available")

    LOCK.acquire()
    global EXPERIMENT
    EXPERIMENT = None
    LOCK.release()


@app.post("/init")
def init():
    # TODO: Move to create_app method
    CONFIG.origin = SUPERVISOR_ID

    cluster = request.json["cluster"]
    endpoint = request.json["endpoint"]

    supervisor = Supervisor(SUPERVISOR_ID, None, cluster, endpoint, "available")

    db.add_supervisors([supervisor])

    return asdict(supervisor)


@app.route("/update")
def update():
    # TODO: Move to create_app method
    CONFIG.origin = SUPERVISOR_ID

    global EXPERIMENT

    LOCK.acquire()
    if EXPERIMENT is not None:
        experiment_d = asdict(EXPERIMENT)
        LOCK.release()
        return {
            "status": "ok",
            "info": "There is an experiment running already",
            "experiment": experiment_d,
        }

    else:
        EXPERIMENT = db.get_assignment(SUPERVISOR_ID)
        experiment = EXPERIMENT
        LOCK.release()

    if experiment is None:
        db.set_supervisor_status(SUPERVISOR_ID, "available")
        return {"status": "ok", "info": "No assigned experiments"}

    threading.Thread(target=run_experiment, args=(experiment,)).start()

    return {
        "status": "ok",
        "info": "Running experiment",
        "experiment": asdict(experiment),
    }


def run_supervisor():
    app.run("0.0.0.0", 8000)


class SupervisorClient:
    def __init__(self, url):
        self.url = url
        self.conn = requests.Session()

    def init(self, cluster, endpoint):
        result = self.conn.post(
            self.url + "/init", json={"cluster": cluster, "endpoint": endpoint}
        )
        return result.json()

    def status(self):
        result = self.conn.get(self.url + "/status")
        return result.json()

    def update(self):
        result = self.conn.get(self.url + "/update")
        return result.json()


if __name__ == "__main__":
    client = SupervisorClient("http://10.141.0.11:8000")
    print(client.init("cluster", "http://10.141.0.11:8000"))
    # print(client.status())
    # print(client.update())
