import datetime as dt
import logging
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import groupby

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import ml, workspace
from databricks.sdk.service.workspace import ObjectInfo, ObjectType
from ratelimit import limits, sleep_and_retry

from databricks.labs.ucx.inventory.types import RequestObjectType
from databricks.labs.ucx.support.permissions import GenericPermissionsInfo

logger = logging.getLogger(__name__)


class WorkspaceListing:
    def __init__(
        self,
        ws: WorkspaceClient,
        num_threads: int,
        *,
        with_directories: bool = True,
    ):
        self.start_time = None
        self._ws = ws
        self.results: list[ObjectInfo] = []
        self._num_threads = num_threads
        self._with_directories = with_directories
        self._counter = 0

    def _progress_report(self, _):
        self._counter += 1
        measuring_time = dt.datetime.now()
        delta_from_start = measuring_time - self.start_time
        rps = self._counter / delta_from_start.total_seconds()
        directory_count = len([r for r in self.results if r.object_type == ObjectType.DIRECTORY])
        other_count = len([r for r in self.results if r.object_type != ObjectType.DIRECTORY])
        if self._counter % 10 == 0:
            logger.info(
                f"Made {self._counter} workspace listing calls, "
                f"collected {len(self.results)} objects ({directory_count} dirs and {other_count} other objects),"
                f" rps: {rps:.3f}/sec"
            )

    @sleep_and_retry
    @limits(calls=45, period=1)  # safety value, can be 50 actually
    def _list_workspace(self, path: str) -> Iterator[ObjectType]:
        # TODO: remove, use SDK
        return self._ws.workspace.list(path=path, recursive=False)

    def _list_and_analyze(self, obj: ObjectInfo) -> (list[ObjectInfo], list[ObjectInfo]):
        directories = []
        others = []
        grouped_iterator = groupby(self._list_workspace(obj.path), key=lambda x: x.object_type == ObjectType.DIRECTORY)
        for is_directory, objects in grouped_iterator:
            if is_directory:
                directories.extend(list(objects))
            else:
                others.extend(list(objects))

        logger.debug(f"Listed {obj.path}, found {len(directories)} sub-directories and {len(others)} other objects")
        return directories, others

    def walk(self, start_path="/"):
        self.start_time = dt.datetime.now()
        logger.info(f"Recursive WorkspaceFS listing started at {self.start_time}")
        root_object = self._ws.workspace.get_status(start_path)
        self.results.append(root_object)

        with ThreadPoolExecutor(self._num_threads) as executor:
            initial_future = executor.submit(self._list_and_analyze, root_object)
            initial_future.add_done_callback(self._progress_report)
            futures_to_objects = {initial_future: root_object}
            while futures_to_objects:
                futures_done, futures_not_done = wait(futures_to_objects, return_when=FIRST_COMPLETED)

                for future in futures_done:
                    futures_to_objects.pop(future)
                    directories, others = future.result()
                    self.results.extend(directories)
                    self.results.extend(others)

                    if directories:
                        new_futures = {}
                        for directory in directories:
                            new_future = executor.submit(self._list_and_analyze, directory)
                            new_future.add_done_callback(self._progress_report)
                            new_futures[new_future] = directory
                        futures_to_objects.update(new_futures)

            logger.info(f"Recursive WorkspaceFS listing finished at {dt.datetime.now()}")
            logger.info(f"Total time taken for workspace listing: {dt.datetime.now() - self.start_time}")
            self._progress_report(None)
        return self.results


def models_listing(ws: WorkspaceClient):
    def inner() -> Iterator[ml.ModelDatabricks]:
        for model in ws.model_registry.list_models():
            model_with_id = ws.model_registry.get_model(model.name).registered_model_databricks
            yield model_with_id

    return inner


def experiments_listing(ws: WorkspaceClient):
    def inner() -> Iterator[ml.Experiment]:
        for experiment in ws.experiments.list_experiments():
            """
            We filter-out notebook-based experiments, because they are covered by notebooks listing
            """
            # workspace-based notebook experiment
            if experiment.tags:
                nb_tag = [t for t in experiment.tags if t.key == "mlflow.experimentType" and t.value == "NOTEBOOK"]
                # repo-based notebook experiment
                repo_nb_tag = [
                    t for t in experiment.tags if t.key == "mlflow.experiment.sourceType" and t.value == "REPO_NOTEBOOK"
                ]
                if nb_tag or repo_nb_tag:
                    continue

            yield experiment

    return inner


def authorization_listing():
    def inner():
        for _value in ["passwords", "tokens"]:
            yield GenericPermissionsInfo(
                object_id=_value,
                request_type=RequestObjectType.AUTHORIZATION,
            )

    return inner


def _convert_object_type_to_request_type(_object: workspace.ObjectInfo) -> RequestObjectType | None:
    match _object.object_type:
        case workspace.ObjectType.NOTEBOOK:
            return RequestObjectType.NOTEBOOKS
        case workspace.ObjectType.DIRECTORY:
            return RequestObjectType.DIRECTORIES
        case workspace.ObjectType.LIBRARY:
            return None
        case workspace.ObjectType.REPO:
            return RequestObjectType.REPOS
        case workspace.ObjectType.FILE:
            return RequestObjectType.FILES
        # silent handler for experiments - they'll be inventorized by the experiments manager
        case None:
            return None


def workspace_listing(ws: WorkspaceClient, num_threads=20, start_path: str | None = "/"):
    def inner():
        ws_listing = WorkspaceListing(
            ws,
            num_threads=num_threads,
            with_directories=False,
        )
        for _object in ws_listing.walk(start_path):
            request_type = _convert_object_type_to_request_type(_object)
            if request_type:
                yield GenericPermissionsInfo(object_id=str(_object.object_id), request_type=request_type)

    return inner