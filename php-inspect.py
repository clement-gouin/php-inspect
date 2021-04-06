import os
import re
from time import time
from configparser import ConfigParser


re.count = lambda pattern, string: len(re.findall(pattern, string))


def dir_walk(*paths, file_filter=None):
    if len(paths) != 1:
        return [os.path.join(path, item) for path in paths for item in dir_walk(path)]
    path = paths[0]
    dir_items = []
    items = []
    for item in sorted(os.listdir(path)):
        subpath = os.path.join(path, item)
        if os.path.isfile(subpath):
            if file_filter is None or re.match(file_filter, item):
                items += [subpath]
        else:
            dir_items += [
                os.path.join(subpath, subitem)
                for subitem in dir_walk(subpath, file_filter=file_filter)
            ]
    return dir_items + items


class DB:
    def __init__(self, root_path, entrypoint_paths):
        self.root_path = os.path.realpath(root_path)
        self.entrypoint_paths = [
            os.path.realpath(os.path.join(root_path, path)) for path in entrypoint_paths
        ]
        self.files = []
        self.entrypoints = []
        self.classes = {}

    def init(self):
        filenames = dir_walk(self.root_path, file_filter=r".*.php")
        self.files = [File(filename, False) for filename in filenames]

        self.entrypoints = dir_walk(*self.entrypoint_paths, file_filter=r".*.php")
        self.files += [File(filename, True) for filename in self.entrypoints]

    def load(self):
        for file in self.files:
            file.load(self.root_path)

        self.classes = {
            file.full_classname: file for file in self.files if file.is_class
        }

    def scan(self):
        for file in self.classes.values():
            file.analyse(self)
        self.invalid = [
            file
            for file in self.classes.values()
            if len(file.callers) == 0 and not file.is_used
        ]
        self.unused = [file for file in self.classes.values() if not file.is_used]
        self.used = [file for file in self.classes.values() if file.is_used]
        self.unused_func_count = sum(
            [len(file.get_unused_functions()) for file in self.used]
        )


class File:
    ignored = []

    def __init__(self, filename, is_entrypoint):
        self.filename = filename
        self.content = None
        self.is_entrypoint = is_entrypoint
        self.lines = []
        self.classname = None
        self.namespace = None
        self.full_classname = None
        self.type = None
        self.raw_imports = []
        self.alias_imports = {}
        self.functions = []
        self.callers = []
        self.called = []
        self._imports = None
        self._is_used = None

    def load(self, root_path):
        with open(os.path.join(root_path, self.filename)) as f:
            self.content = f.read()
        if not self.is_entrypoint:
            self.lines = self.content.split("\n")
            for line in self.lines:
                line = line.strip()
                m1 = re.match(r"^namespace (App\\[\\\w]+);$", line)
                m2 = re.match(r"^use (App\\[\\\w]+)( as (\w+))?;$", line)
                m3 = re.match(r"^(abstract\s+)?(class|interface|trait)\s+(\w+)", line)
                m4 = re.match(r"^(public|protected|private)\s+function\s+(\w+)", line)
                if m1:
                    self.namespace = m1.groups()[0]
                if m2:
                    self.raw_imports += [m2.groups()[0]]
                    if m2.groups()[2]:
                        self.alias_imports[self.raw_imports[-1]] = m2.groups()[2]
                if m3:
                    self.classname = m3.groups()[-1]
                    self.type = m3.groups()[-2]
                if m4 and not m4.groups()[1].startswith("__"):
                    self.functions += [Function(m4.groups()[0], m4.groups()[1])]
            if self.is_class:
                self.full_classname = f"{self.namespace}\\{self.classname}"

    def analyse(self, db):
        if self.is_class:
            public_func = [
                func
                for func in self.functions
                if func.type == "public" or func.type == "protected"
            ]
            for file in db.files:
                if file.filename != self.filename:
                    if file.is_calling(db, self):
                        self.callers += [file]
                        file.called += [self]
                    for func in public_func:
                        if re.search(func.name, file.content):
                            func.callers += [file]
                else:
                    for func in self.functions:
                        if re.count(func.name, self.content) >= 2:
                            func.callers += [self]

    def is_calling(self, db, other_file):
        if self.is_class:
            for imp in self.get_imports(db):
                if imp == other_file:
                    if imp.full_classname in self.alias_imports:
                        to_find = self.alias_imports[imp.full_classname]
                        return re.count(to_find, self.content) >= 2
                    to_detect = 3 if other_file.classname in self.classname else 2
                    # import + (classname) + usage
                    return re.count(other_file.classname, self.content) >= to_detect
                if other_file.classname in imp.classname:
                    return False
            if other_file.classname in self.classname:
                return False
            return re.search(other_file.classname, self.content)
        else:
            return re.search(other_file.classname, self.content)

    def is_used_full(self, scanned):
        if not self.is_class or self.full_classname in self.__class__.ignored:
            return True
        if self._is_used is None:
            for ign in self.__class__.ignored:
                if self.full_classname.startswith(ign):
                    self._is_used = True
                    break
            else:
                for caller in self.callers:
                    if caller not in scanned and caller.is_used_full(scanned + [self]):
                        self._is_used = True
                        break
                else:
                    self._is_used = False
        return self._is_used

    def get_imports(self, db):
        if self._imports is None:
            self._imports = [
                db.classes[imp] for imp in self.raw_imports if imp in db.classes
            ]
        return self._imports

    def get_unused_functions(self):
        return [func for func in self.functions if not func.is_used]

    @property
    def is_used(self):
        return self.is_used_full([self])

    @property
    def is_class(self):
        return (
            not self.is_entrypoint
            and self.namespace is not None
            and self.classname is not None
        )

    def __repr__(self):
        if self.is_class:
            return f"{self.type} {self.full_classname} ({len(self.functions)} functions, {(len(self.callers))} callers)"
        elif self.is_entrypoint:
            return f"entrypoint - {self.filename}"
        else:
            return f"unknown - {self.filename}"


class Function:
    def __init__(self, type, name):
        if name.startswith("scope"):
            self.name = name[5].lower() + name[6:]
        else:
            self.name = name
        self.type = type
        self.callers = []

    @property
    def is_used(self):
        if len(self.callers) == 0:
            return False
        for caller in self.callers:
            if caller.is_used:
                return True
        return False

    def __repr__(self):
        return f"{self.type} function {self.name} ({len(self.callers)} callers)"


def time_print(t0, message):
    print(f"({1000*(time()-t0):.1f}ms) {message}")


def print_branch(file, level=0, found=[]):
    if level == 0:
        print(file.full_classname)
    else:
        print((level - 1) * 2 * " ", "∟", file.full_classname)
    found += [file]
    for called in file.called:
        if not called.is_used and called not in found:
            print_branch(called, level + 1, found)


def print_invalid_branches(db):
    print("\n\n====IGNORED====")
    for name in File.ignored:
        print(name)
    print("\n\n====INVALID BRANCHES====")
    found = []
    for file in db.invalid:
        print_branch(file, found=found)


def print_unused_functions(db):
    print("\n\n====UNUSED FUNCTIONS====")
    for file in db.used:
        funcs = file.get_unused_functions()
        if len(funcs) > 0:
            print(file)
            for func in funcs:
                print("->", func)
            print()


def print_specific(db, names):
    print("\n\n====SPECIFIC CLASSES====")
    for name in names:
        if name not in db.classes:
            return
        file = db.classes[name]
        print(file)
        print("callers:")
        for caller in file.callers:
            if caller.is_used:
                print("->", caller.full_classname)
            else:
                print("x>", caller.full_classname)
        print()


def read_config_list(config, section, option):
    val = config.get(section, option)
    return [v.strip() for v in val.splitlines() if len(v.strip()) > 0]


def write_output(db, filename):
    t0 = time()
    with open(filename, mode="w") as f:
        f.write("\n".join([file.filename for file in db.unused]))
    time_print(t0, f"wrote {len(db.unused)} lines in {filename}")


def remove_file(file, level=0, to_delete=[], force=False):
    if level == 0:
        text = f"{file.full_classname} => delete (yes/no/all/cancel/recursive) (n)? "
    else:
        text = f"{(level - 1) * 2 * ' ' }∟ {file.full_classname} => delete (yes/no/all/cancel/recursive) (n)? "
    if force:
        print(text + "r")
        choice = "r"
    else:
        choice = input(text)
        choice = choice.lower()[0] if choice else "n"
    if choice == "a" or choice == "c":
        return choice
    elif choice == "y" or choice == "r":
        to_delete += [file]
        for called in file.called:
            if not called.is_used and called not in to_delete:
                for caller in called.callers:
                    if caller not in to_delete:
                        break
                else:
                    stop = remove_file(called, level + 1, to_delete, choice == "r")
                    if stop is not None:
                        return stop
    return None


def remove_files(db):
    to_delete = []
    for file in db.invalid:
        stop = remove_file(file, to_delete=to_delete)
        if stop == "a":
            to_delete = db.unused
            break
        elif stop == "c":
            to_delete = []
            break
    t0 = time()
    for file in to_delete:
        os.unlink(file.filename)
    time_print(t0, f"removed {len(to_delete)} files")


def main():
    if not os.path.exists("config.ini"):
        print("config.ini not found")
        exit(1)
        return

    config = ConfigParser()
    config.read("config.ini")

    config.getlist = lambda section, option: read_config_list(config, section, option)

    db = DB(config.get("input", "root_path"), config.getlist("input", "entrypoints"))

    File.ignored = config.getlist("input", "ignored")

    t0 = time()
    db.init()
    time_print(
        t0, f"found {len(db.files)} files with {len(db.entrypoints)} entrypoint files"
    )

    t0 = time()
    db.load()
    time_print(t0, f"loaded {len(db.classes)} classes")

    t0 = time()
    db.scan()
    time_print(
        t0,
        f"scanned classes and found {len(db.invalid)} invalid roots for {len(db.unused)} unused files and {db.unused_func_count} unused functions",
    )

    if config.getboolean("output", "print_invalid"):
        print_invalid_branches(db)

    if config.getboolean("output", "print_functions"):
        print_unused_functions(db)

    if config.getboolean("output", "print_specific"):
        print_specific(db, config.getlist("output", "to_scan"))

    if config.get("output", "output_file"):
        write_output(db, config.get("output", "output_file"))

    if config.getboolean("output", "remove_files"):
        remove_files(db)


main()
