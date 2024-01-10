from pathlib import Path
from typing import Optional


def _flamingo_check_config(config: dict) -> dict:
    data_path = Path(config["data_path"])

    region = data_path.name
    slide = data_path.parent.name
    tissue = data_path.parents[1].name

    config["cold_data_path"] = Path("/mnt/glustergv0/MERFISH/data") / tissue / slide / region
    config["processed_data_path"] = (
        Path("/mnt/glustergv0/MERFISH/processed") / tissue / slide / f"{region}.explorer"
    )

    return config


def _sanity_check_config(config: dict):
    config = _flamingo_check_config(config)

    assert (
        "data_path" in config or "sdata_path" in config
    ), "Invalid config. Provide '--config data_path=...' when running the pipeline"

    if "data_path" in config and not "sdata_path" in config:
        config["sdata_path"] = Path(config["data_path"]).with_suffix(".zarr")
        print(
            f"SpatialData object path set to default: {config['sdata_path']}\nTo change this behavior, provide `--config sdata_path=...` when running the snakemake pipeline"
        )

    if "data_path" not in config:
        assert Path(
            config["sdata_path"]
        ).exists(), f"When `data_path` is not provided, the spatial data object must exist, but the directory doesn't exists: {config['sdata_path']}"
        config["data_path"] = []

    return config


class WorkflowPaths:
    def __init__(self, config: dict) -> None:
        self.config = _sanity_check_config(config)

        # spatialdata object files
        self.sdata_path = Path(self.config["sdata_path"])
        self.data_path = self.config["data_path"]
        self.sdata_zgroup = self.sdata_path / ".zgroup"  # trick to fix snakemake ChildIOException

        # spatial elements directories
        self.shapes_dir = self.sdata_path / "shapes"
        self.points_dir = self.sdata_path / "points"
        self.images_dir = self.sdata_path / "images"
        self.table_dir = self.sdata_path / "table"

        # sdata.shapes
        self.baysor_boundaries = self.shapes_dir / "baysor_boundaries"
        self.cellpose_boundaries = self.shapes_dir / "cellpose_boundaries"
        self.patches = self.shapes_dir / "sopa_patches"

        # snakemake cache / intermediate files
        self.sopa_cache = self.sdata_path / ".sopa_cache"
        self.smk_table = self.sopa_cache / "table"
        self.smk_patches = self.sopa_cache / "patches"
        self.smk_patches_file_image = self.sopa_cache / "patches_file_image"
        self.smk_patches_file_baysor = self.sopa_cache / "patches_file_baysor"
        self.smk_cellpose_temp_dir = self.sopa_cache / "cellpose_boundaries"
        self.smk_baysor_temp_dir = self.sopa_cache / "baysor_boundaries"
        self.smk_cellpose_boundaries = self.sopa_cache / "cellpose_boundaries_done"
        self.smk_baysor_boundaries = self.sopa_cache / "baysor_boundaries_done"
        self.smk_aggregation = self.sopa_cache / "aggregation"

        # annotation files
        self.annotations = []
        if "annotation" in self.config:
            key = self.config["annotation"].get("args", {}).get("cell_type_key", "cell_type")
            self.annotations = self.table_dir / "table" / "obs" / key

        # user-friendly output files
        self.explorer_directory = self.sdata_path.with_suffix(".explorer")
        self.explorer_directory.mkdir(parents=True, exist_ok=True)
        self.explorer_experiment = self.explorer_directory / "experiment.xenium"
        self.explorer_image = self.explorer_directory / "morphology.ome.tif"
        self.report = self.explorer_directory / "analysis_summary.html"

        self.processed_adata = str(self.config["processed_data_path"] / "adata.h5ad")

    def cells_paths(self, file_content: str, name, dirs: bool = False):
        if name == "cellpose":
            return [
                str(self.smk_cellpose_temp_dir / f"{i}.parquet") for i in range(int(file_content))
            ]
        if name == "baysor":
            indices = map(int, file_content.split())
            BAYSOR_FILES = ["segmentation_polygons.json", "segmentation_counts.loom"]

            if dirs:
                return [str(self.smk_baysor_temp_dir / str(i)) for i in indices]
            return [
                str(self.smk_baysor_temp_dir / str(i) / file)
                for i in indices
                for file in BAYSOR_FILES
            ]


class Args:
    def __init__(self, paths: WorkflowPaths, config: dict):
        self.paths = paths
        self.config = config

        self.segmentation = "segmentation" in self.config
        self.cellpose = self.segmentation and "cellpose" in self.config["segmentation"]
        self.baysor = self.segmentation and "baysor" in self.config["segmentation"]
        self.annotate = "annotation" in self.config and "method" in self.config["annotation"]

        if self.baysor:
            assert (
                "executables" in config and "baysor" in config["executables"]
            ), """When using baysor, please provide the path to the baysor executable in the config["executables"]["baysor"]"""
            baysor_path = Path(config["executables"]["baysor"]).expanduser()
            assert baysor_path.exists(), f"""Baysor executable {baysor_path} does not exist.\
                \nCheck that you have installed baysor executable (as in https://github.com/kharchenkolab/Baysor), or update config["executables"]["baysor"] to use the right executable location"""

    def __getitem__(self, name):
        subconfig = self.config.get(name, {})
        if not isinstance(subconfig, dict):
            return subconfig
        return Args(self.paths, subconfig)

    def where(self, keys: Optional[list[str]] = None, contains: Optional[str] = None):
        if keys is not None:
            return Args(self.paths, {key: self.config[key] for key in keys if key in self.config})
        if contains is not None:
            return Args(
                self.paths,
                {key: value for key, value in self.config.items() if contains in key},
            )
        return self

    def dump_baysor_patchify(self):
        return (
            str(self["segmentation"]["baysor"])
            + f" --baysor-temp-dir {self.paths.smk_baysor_temp_dir}"
        )

    @classmethod
    def _dump_arg(cls, key: str, value, prefix: str = ""):
        option = f"--{prefix}{key.replace('_', '-')}"
        if isinstance(value, list):
            for v in value:
                yield from (option, stringify_for_cli(v))
        elif isinstance(value, dict):
            yield from (option, '"' + stringify_for_cli(value) + '"')
        elif value is True:
            yield option
        elif value is False:
            yield f"--no-{prefix}{key.replace('_', '-')}"
        else:
            yield from (option, stringify_for_cli(value))

    def _dump(self, prefix=""):
        return " ".join(
            (res for item in self.config.items() for res in self._dump_arg(*item, prefix))
        )

    def __str__(self) -> str:
        return self._dump()

    @property
    def baysor_prior_seg(self):
        if not self.baysor:
            return ""

        key = self.config["segmentation"]["baysor"].get("cell_key")
        if key is not None:
            return f":{key}"

        if "cellpose" in self.config["segmentation"]:
            return ":cell"

        return ""

    def min_area(self, method):
        params = self.config[method]
        if "min_area" not in params:
            return 0
        return params["min_area"]

    @property
    def gene_column(self):
        return self.config["segmentation"]["baysor"]["config"]["data"]["gene"]


def stringify_for_cli(value) -> str:
    if isinstance(value, str):
        return f"'{value}'"
    return str(value)
