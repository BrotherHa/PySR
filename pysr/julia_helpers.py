"""Functions for initializing the Julia environment and installing deps."""
import sys
import subprocess
import warnings
from pathlib import Path
import os
from julia.api import JuliaError

from .version import __version__, __symbolic_regression_jl_version__

juliainfo = None
julia_initialized = False
julia_kwargs_at_initialization = None
julia_activated_env = None


def _load_juliainfo():
    """Execute julia.core.JuliaInfo.load(), and store as juliainfo."""
    global juliainfo

    if juliainfo is None:
        from julia.core import JuliaInfo

        try:
            juliainfo = JuliaInfo.load(julia="julia")
        except FileNotFoundError:
            env_path = os.environ["PATH"]
            raise FileNotFoundError(
                f"Julia is not installed in your PATH. Please install Julia and add it to your PATH.\n\nCurrent PATH: {env_path}",
            )

    return juliainfo


def _get_julia_project_dir():
    # Assumes it is in JULIA_PROJECT:
    assert "JULIA_PROJECT" in os.environ and os.environ["JULIA_PROJECT"] != ""
    try:
        cmds = [
            "julia",
            "-e using Pkg; print(Pkg.project().path)",
        ]
        julia_project_dir_str = subprocess.run(
            cmds,
            capture_output=True,
            env=os.environ,
        ).stdout.decode()
    except FileNotFoundError:
        env_path = os.environ["PATH"]
        raise FileNotFoundError(
            f"Julia is not installed in your PATH. Please install Julia and add it to your PATH.\n\nCurrent PATH: {env_path}",
        )
    return Path(julia_project_dir_str).parent


def _get_julia_env_dir():
    # Have to manually get env dir:
    try:
        julia_env_dir_str = subprocess.run(
            ["julia", "-e using Pkg; print(Pkg.envdir())"],
            capture_output=True,
            env=os.environ,
        ).stdout.decode()
    except FileNotFoundError:
        env_path = os.environ["PATH"]
        raise FileNotFoundError(
            f"Julia is not installed in your PATH. Please install Julia and add it to your PATH.\n\nCurrent PATH: {env_path}",
        )
    return Path(julia_env_dir_str)


def _set_julia_project_env(julia_project, is_shared):
    if is_shared:
        if is_julia_version_greater_eq(version=(1, 7, 0)):
            os.environ["JULIA_PROJECT"] = "@" + str(julia_project)
        else:
            julia_env_dir = _get_julia_env_dir()
            os.environ["JULIA_PROJECT"] = str(julia_env_dir / julia_project)
    else:
        os.environ["JULIA_PROJECT"] = str(julia_project)


def _get_io_arg(quiet):
    io = "devnull" if quiet else "stderr"
    io_arg = f"io={io}" if is_julia_version_greater_eq(version=(1, 6, 0)) else ""
    return io_arg


def _import_error():
    return """
    Required dependencies are not installed or built.  Run the following code in the Python REPL:

        >>> import pysr
        >>> pysr.install()
    """


def _process_julia_project(julia_project):
    if julia_project is None:
        is_shared = True
        processed_julia_project = f"pysr-{__version__}"
    elif julia_project[0] == "@":
        is_shared = True
        processed_julia_project = julia_project[1:]
    else:
        is_shared = False
        processed_julia_project = Path(julia_project)
    return processed_julia_project, is_shared


def is_julia_version_greater_eq(juliainfo=None, version=(1, 6, 0)):
    """Check if Julia version is greater than specified version."""
    if juliainfo is None:
        juliainfo = _load_juliainfo()
    current_version = (
        juliainfo.version_major,
        juliainfo.version_minor,
        juliainfo.version_patch,
    )
    return current_version >= version


def _check_for_conflicting_libraries():  # pragma: no cover
    """Check whether there are conflicting modules, and display warnings."""
    # See https://github.com/pytorch/pytorch/issues/78829: importing
    # pytorch before running `pysr.fit` causes a segfault.
    torch_is_loaded = "torch" in sys.modules
    if torch_is_loaded:
        warnings.warn(
            "`torch` was loaded before the Julia instance started. "
            "This may cause a segfault when running `PySRRegressor.fit`. "
            "To avoid this, please run `pysr.julia_helpers.init_julia()` *before* "
            "importing `torch`. "
            "For updates, see https://github.com/pytorch/pytorch/issues/78829"
        )


def init_julia(julia_project=None, quiet=False, sysimage_name=None, julia_kwargs=None, return_aux=False):
    """Initialize julia binary, turning off compiled modules if needed."""
    global julia_initialized
    global julia_kwargs_at_initialization
    global julia_activated_env

    if not julia_initialized:
        _check_for_conflicting_libraries()

    _julia_version_assertion()
    processed_julia_project, is_shared = _process_julia_project(julia_project)
    _set_julia_project_env(processed_julia_project, is_shared)

    # TODO: Make checking optional.
    # Check if sysimage exists:
    if sysimage_name is None and not julia_initialized:
        # TODO: Is there a faster way to get this dir?
        expected_sysimage = _get_julia_project_dir() / "pysr.so"
        # Check if this file exists:
        if expected_sysimage.exists():
            sysimage_name = str(expected_sysimage)

    if julia_kwargs is None:
        julia_kwargs = {"optimize": 3}

    if sysimage_name is not None and "sysimage" not in julia_kwargs and not julia_initialized:
        sysimage = str(sysimage_name)
        print(f"Found existing sysimage at {sysimage}. Loading.")
        julia_kwargs["sysimage"] = sysimage_name

    from julia.core import JuliaInfo, UnsupportedPythonError

    try:
        # TODO: Can we just get env info from this?
        info = JuliaInfo.load(julia="julia")
    except FileNotFoundError:
        env_path = os.environ["PATH"]
        raise FileNotFoundError(
            f"Julia is not installed in your PATH. Please install Julia and add it to your PATH.\n\nCurrent PATH: {env_path}",
        )

    if not info.is_pycall_built():
        raise ImportError(_import_error())

    from julia.core import Julia

    try:
        Julia(**julia_kwargs)
    except UnsupportedPythonError:
        # Static python binary, so we turn off pre-compiled modules.
        julia_kwargs = {**julia_kwargs, "compiled_modules": False}
        Julia(**julia_kwargs)

    using_compiled_modules = (not "compiled_modules" in julia_kwargs) or julia_kwargs[
        "compiled_modules"
    ]

    from julia import Main as _Main

    Main = _Main

    if julia_activated_env is None:
        julia_activated_env = processed_julia_project

    if julia_initialized and julia_kwargs_at_initialization is not None:
        # Check if the kwargs are the same as the previous initialization
        init_set = set(julia_kwargs_at_initialization.items())
        new_set = set(julia_kwargs.items())
        set_diff = new_set - init_set
        # Remove the `compiled_modules` key, since it is not a user-specified kwarg:
        set_diff = {k: v for k, v in set_diff if k != "compiled_modules"}
        if len(set_diff) > 0:
            warnings.warn(
                "Julia has already started. The new Julia options "
                + str(set_diff)
                + " will be ignored."
            )

    if julia_initialized and julia_activated_env != processed_julia_project:
        Main.eval("using Pkg")

        io_arg = _get_io_arg(quiet)
        # Can't pass IO to Julia call as it evaluates to PyObject, so just directly
        # use Main.eval:
        Main.eval(
            f'Pkg.activate("{_escape_filename(processed_julia_project)}",'
            f"shared = Bool({int(is_shared)}), "
            f"{io_arg})"
        )

        julia_activated_env = processed_julia_project

    if not julia_initialized:
        julia_kwargs_at_initialization = julia_kwargs

    julia_initialized = True
    if return_aux:
        return Main, {"compiled_modules": using_compiled_modules}
    return Main


def _escape_filename(filename):
    """Turn a path into a string with correctly escaped backslashes."""
    str_repr = str(filename)
    str_repr = str_repr.replace("\\", "\\\\")
    return str_repr


def _julia_version_assertion():
    if not is_julia_version_greater_eq(version=(1, 6, 0)):
        raise NotImplementedError(
            "PySR requires Julia 1.6.0 or greater. "
            "Please update your Julia installation."
        )


def _backend_version_assertion(Main):
    try:
        backend_version = Main.eval("string(SymbolicRegression.PACKAGE_VERSION)")
        expected_backend_version = __symbolic_regression_jl_version__
        if backend_version != expected_backend_version:  # pragma: no cover
            warnings.warn(
                f"PySR backend (SymbolicRegression.jl) version {backend_version} "
                f"does not match expected version {expected_backend_version}. "
                "Things may break. "
                "Please update your PySR installation with "
                "`python -c 'import pysr; pysr.install()'`."
            )
    except JuliaError:  # pragma: no cover
        warnings.warn(
            "You seem to have an outdated version of SymbolicRegression.jl. "
            "Things may break. "
            "Please update your PySR installation with "
            "`python -c 'import pysr; pysr.install()'`."
        )


def _load_cluster_manager(Main, cluster_manager):
    Main.eval(f"import ClusterManagers: addprocs_{cluster_manager}")
    return Main.eval(f"addprocs_{cluster_manager}")


def _load_backend(Main):
    try:
        # Load namespace, so that various internal operators work:
        Main.eval("using SymbolicRegression")
    except (JuliaError, RuntimeError) as e:
        raise ImportError(_import_error()) from e

    _backend_version_assertion(Main)

    # Load Julia package SymbolicRegression.jl
    from julia import SymbolicRegression

    return SymbolicRegression
