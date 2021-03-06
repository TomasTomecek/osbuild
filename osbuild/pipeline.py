
import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
from . import buildroot
from . import objectstore
from . import remoteloop


RESET = "\033[0m"
BOLD = "\033[1m"


class StageFailed(Exception):
    def __init__(self, name, returncode, output):
        super(StageFailed, self).__init__()
        self.name = name
        self.returncode = returncode
        self.output = output


class AssemblerFailed(Exception):
    def __init__(self, name, returncode, output):
        super(AssemblerFailed, self).__init__()
        self.name = name
        self.returncode = returncode
        self.output = output


def print_header(title, options):
    print()
    print(f"{RESET}{BOLD}{title}{RESET} " + json.dumps(options or {}, indent=2))
    print()


class Stage:
    def __init__(self, name, build, base, options):
        m = hashlib.sha256()
        m.update(json.dumps(name, sort_keys=True).encode())
        m.update(json.dumps(build, sort_keys=True).encode())
        m.update(json.dumps(base, sort_keys=True).encode())
        m.update(json.dumps(options, sort_keys=True).encode())

        self.id = m.hexdigest()
        self.name = name
        self.options = options

    def description(self):
        description = {}
        description["name"] = self.name
        if self.options:
            description["options"] = self.options
        return description

    def run(self, tree, build_tree, interactive=False, check=True, libdir=None):
        with buildroot.BuildRoot(build_tree) as build_root:
            if interactive:
                print_header(f"{self.name}: {self.id}", self.options)

            args = {
                "tree": "/run/osbuild/tree",
                "options": self.options,
            }

            path = "/run/osbuild/lib" if libdir else "/usr/libexec/osbuild"
            r = build_root.run(
                [f"{path}/osbuild-run", f"{path}/stages/{self.name}"],
                binds=[f"{tree}:/run/osbuild/tree"],
                readonly_binds=[f"{libdir}:{path}"] if libdir else [],
                encoding="utf-8",
                input=json.dumps(args),
                stdout=None if interactive else subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            if check and r.returncode != 0:
                raise StageFailed(self.name, r.returncode, r.stdout)

            return {
                "name": self.name,
                "returncode": r.returncode,
                "output": r.stdout
            }


class Assembler:
    def __init__(self, name, options):
        self.name = name
        self.options = options

    def description(self):
        description = {}
        description["name"] = self.name
        if self.options:
            description["options"] = self.options
        return description

    def run(self, tree, build_tree, output_dir=None, interactive=False, check=True, libdir=None):
        with buildroot.BuildRoot(build_tree) as build_root:
            if interactive:
                print_header(f"Assembling: {self.name}", self.options)

            args = {
                "tree": "/run/osbuild/tree",
                "options": self.options,
            }

            binds = []
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
                binds.append(f"{output_dir}:/run/osbuild/output")
                args["output_dir"] = "/run/osbuild/output"

            path = "/run/osbuild/lib" if libdir else "/usr/libexec/osbuild"
            with build_root.bound_socket("remoteloop") as sock, \
                remoteloop.LoopServer(sock):
                r = build_root.run(
                    [f"{path}/osbuild-run", f"{path}/assemblers/{self.name}"],
                    binds=binds,
                    readonly_binds=[f"{tree}:/run/osbuild/tree"] + ([f"{libdir}:{path}"] if libdir else []),
                    encoding="utf-8",
                    input=json.dumps(args),
                    stdout=None if interactive else subprocess.PIPE,
                    stderr=subprocess.STDOUT)
                if check and r.returncode != 0:
                    raise AssemblerFailed(self.name, r.returncode, r.stdout)

            return {
                "name": self.name,
                "returncode": r.returncode,
                "output": r.stdout
            }


class Pipeline:
    def __init__(self, build=None):
        self.build = build
        self.stages = []
        self.assembler = None

    def get_id(self):
        return self.stages[-1].id if self.stages else None

    def add_stage(self, name, options=None):
        build = self.build.get_id() if self.build else None
        stage = Stage(name, build, self.get_id(), options or {})
        self.stages.append(stage)

    def set_assembler(self, name, options=None):
        self.assembler = Assembler(name, options or {})

    def prepend_build_pipeline(self, build):
        pipeline = self
        while pipeline.build:
            pipeline = pipeline.build
        pipeline.build = build

    def description(self):
        description = {}
        if self.build:
            description["build"] = self.build.description()
        if self.stages:
            description["stages"] = [s.description() for s in self.stages]
        if self.assembler:
            description["assembler"] = self.assembler.description()
        return description

    @contextlib.contextmanager
    def get_buildtree(self, object_store):
        if self.build:
            with object_store.get_tree(self.build.get_id()) as tree:
                yield tree
        else:
            with tempfile.TemporaryDirectory(dir=object_store.store) as tmp:
                subprocess.run(["mount", "-o", "bind,ro,mode=0755", "/", tmp], check=True)
                try:
                    yield tmp
                finally:
                    subprocess.run(["umount", "--lazy", tmp], check=True)

    def run(self, output_dir, store, interactive=False, check=True, libdir=None):
        os.makedirs("/run/osbuild", exist_ok=True)
        object_store = objectstore.ObjectStore(store)
        results = {
            "stages": []
        }
        if self.build:
            r = self.build.run(None, store, interactive, check, libdir)
            results["build"] = r
            if r["returncode"] != 0:
                results["returncode"] = r["returncode"]
                return results

        with self.get_buildtree(object_store) as build_tree:
            if self.stages:
                if not object_store.has_tree(self.get_id()):
                    # Find the last stage that already exists in the object store, and use
                    # that as the base.
                    base = None
                    base_idx = -1
                    for i in range(len(self.stages) - 1, 0, -1):
                        if object_store.has_tree(self.stages[i].id):
                            base = self.stages[i].id
                            base_idx = i
                            break
                    # The tree does not exist. Create it and save it to the object store. If
                    # two run() calls race each-other, two trees may be generated, and it
                    # is nondeterministic which of them will end up referenced by the tree_id
                    # in the content store. However, we guarantee that all tree_id's and all
                    # generated trees remain valid.
                    with object_store.new_tree(self.get_id(), base_id=base) as tree:
                        for stage in self.stages[base_idx + 1:]:
                            r = stage.run(tree,
                                          build_tree,
                                          interactive=interactive,
                                          check=check,
                                          libdir=libdir)
                            results["stages"].append(r)
                            if r["returncode"] != 0:
                                results["returncode"] = r["returncode"]
                                return results

            if self.assembler:
                with object_store.get_tree(self.get_id()) as tree:
                    r = self.assembler.run(tree,
                                           build_tree,
                                           output_dir=output_dir,
                                           interactive=interactive,
                                           check=check,
                                           libdir=libdir)
                    results["assembler"] = r
                    if r["returncode"] != 0:
                        results["returncode"] = r["returncode"]
                        return results

        results["returncode"] = 0
        return results


def load(description):
    build_description = description.get("build")
    if build_description:
        build = load(build_description)
    else:
        build = None
    pipeline = Pipeline(build)

    for s in description.get("stages", []):
        pipeline.add_stage(s["name"], s.get("options", {}))

    a = description.get("assembler")
    if a:
        pipeline.set_assembler(a["name"], a.get("options", {}))

    return pipeline
