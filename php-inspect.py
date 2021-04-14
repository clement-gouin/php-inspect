import os
import re
from time import time
from configparser import ConfigParser


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
        self.unused = []
        self.used = []
        self.unused_func = []
        self.unused_func_lines = 0

    def init(self):
        filenames = dir_walk(self.root_path, file_filter=r".+\.php$")
        self.files = [File(filename, False) for filename in filenames]

        self.entrypoints = dir_walk(*self.entrypoint_paths, file_filter=r".+\.php$")
        self.files += [File(filename, True) for filename in self.entrypoints]

    def load(self):
        for file in self.files:
            file.load(self.root_path)

        for file in self.files:
            file.find_duplicates(self)

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

        for file in self.classes.values():
            file.analyse_funcs(self)

        self.unused_func = [
            func for file in self.used for func in file.get_unused_functions()
        ]
        self.unused_func_lines = sum([func.lines for func in self.unused_func])


class File:
    ignored = []
    ignored_func = []

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
        self.parent = None

    def load(self, root_path):
        with open(os.path.join(root_path, self.filename)) as f:
            self.content = f.read()
        if not self.is_entrypoint:
            self.lines = self.content.split("\n")
            for i, line in enumerate(self.lines):
                line = line.strip()
                m1 = re.match(r"^namespace (App\\[\\\w]+);$", line)
                m2 = re.match(r"^use (App\\[\\\w]+)( as (\w+))?;$", line)
                m3 = re.match(
                    r"^(abstract\s+)?(class|interface|trait)\s+(\w+)(\s+extends\s+(\w+))?",
                    line,
                )
                m4 = re.match(
                    r"^(abstract\s+)?(public|protected|private)\s+function\s+(\w+)",
                    line,
                )
                if m1:
                    self.namespace = m1.groups()[0]
                if m2:
                    self.raw_imports += [m2.groups()[0]]
                    if m2.groups()[2]:
                        self.alias_imports[self.raw_imports[-1]] = m2.groups()[2]
                if m3:
                    self.classname = m3.groups()[2]
                    self.type = m3.groups()[1]
                    if m3.groups()[4] is not None:
                        self.parent = m3.groups()[4]
                if m4 and not m4.groups()[2].startswith("__"):
                    func = Function(self, m4.groups()[1], m4.groups()[2], i)
                    func.load()
                    self.functions += [func]
            if self.is_class:
                self.full_classname = f"{self.namespace}\\{self.classname}"

    def find_duplicates(self, db):
        if self.is_class:
            dups = [
                file
                for file in db.files
                if file != self
                and file.is_class
                and file.full_classname == self.full_classname
            ]
            if len(dups) > 0:
                print("duplicates for:", self.full_classname)
                print(" ∟", self.filename)
                for file in dups:
                    print(" ∟", file.filename)
                    file.namespace = None

    def analyse(self, db):
        if self.is_class:
            for file in db.files:
                if file.filename != self.filename and file.is_calling(db, self):
                    self.callers += [file]
                    file.called += [self]

    def analyse_funcs(self, db):
        if self.is_class and self.is_used:
            public_func = [
                func
                for func in self.functions
                if func.type == "public" or func.type == "protected"
            ]
            for file in db.files:
                if file == self:
                    for func in self.functions:
                        if file.content.lower().count(func.name.lower()) >= 2:
                            func.callers += [file]
                elif self.parent is not None and self.parent == file.classname:
                    for func in public_func:
                        if file.content.lower().count(func.name.lower()) >= 2:
                            func.callers += [file]
                elif file.parent is not None and file.parent == self.classname:
                    for func in public_func:
                        for other_func in file.functions:
                            if other_func.name == func.name:
                                if file.content.lower().count(func.name.lower()) >= 2:
                                    func.callers += [file]
                                break
                        else:
                            if re.search(func.name.lower(), file.content.lower()):
                                func.callers += [file]
                else:
                    for func in public_func:
                        for other_func in file.functions:
                            if (
                                other_func.name == func.name
                                and file.type != "interface"
                            ):
                                break
                        else:
                            if re.search(func.name.lower(), file.content.lower()):
                                func.callers += [file]

    def is_calling(self, db, other_file):
        if self.is_class:
            for imp in self.get_imports(db):
                if imp == other_file:
                    if imp.full_classname in self.alias_imports:
                        to_find = self.alias_imports[imp.full_classname]
                        return self.content.count(to_find) >= 2
                    to_detect = 3 if other_file.classname in self.classname else 2
                    # import + (classname) + usage
                    return self.content.count(other_file.classname) >= to_detect
                if (
                    other_file.classname in imp.classname
                    and imp.full_classname not in self.alias_imports
                ):
                    return False
            if other_file.classname in self.classname:
                return self.content.count(other_file.classname) >= 2
            return re.search(other_file.classname, self.content)
        else:
            return re.search(other_file.classname, self.content)

    def is_used_full(self, scanned):
        if not self.is_class or self.full_classname in File.ignored:
            return True
        if self._is_used is None:
            for ign in File.ignored:
                if self.full_classname.startswith(ign):
                    self._is_used = True
                    break
            else:
                should_update = True
                for caller in self.callers:
                    if caller not in scanned and caller.is_used_full(scanned + [self]):
                        self._is_used = True
                        break
                    elif caller in scanned:
                        should_update = False
                else:
                    if should_update:
                        self._is_used = False
                    else:
                        return False
        return self._is_used

    def get_imports(self, db):
        if self._imports is None:
            self._imports = [
                db.classes[imp] for imp in self.raw_imports if imp in db.classes
            ]
        return self._imports

    def get_unused_functions(self):
        if self.type == "interface":
            return []
        for ign in File.ignored_func:
            if self.full_classname.startswith(ign):
                return []
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
            infos = [f"{len(self.functions)} functions"]
            if self.is_used:
                infos += [f"{len(self.callers)} callers"]
            elif len(self.callers) > 0:
                infos += [f"{len(self.callers)} unused callers"]
            else:
                infos += [f"unused"]
            if self.parent is not None:
                infos += [f"extends '{self.parent}'"]
            return f"{self.type} {self.full_classname} ({', '.join(infos)})"
        elif self.is_entrypoint:
            return f"entrypoint - {self.filename}"
        else:
            return f"unknown - {self.filename}"


class Function:
    ignored_func_names = []

    def __init__(self, file, type, name, start_line):
        self.file = file
        if name.startswith("scope"):
            self.name = name[5].lower() + name[6:]
        else:
            self.name = name
        self.type = type
        self.callers = []
        self.start_line = start_line
        self.end_line = None
        self.comment_lines = 0

    def load(self):
        if self.file.lines[self.start_line].strip().endswith(";"):
            self.end_line = self.start_line
        else:
            state = 0
            found = False
            for i, line in enumerate(self.file.lines[self.start_line :]):
                state += line.count("{") - line.count("}")
                found = found or line.count("{") > 0
                if state == 0 and found:
                    self.end_line = self.start_line + i
                    break
            if self.file.lines[self.start_line - 1].strip().endswith("*/"):
                while self.start_line >= 0 and not self.file.lines[
                    self.start_line
                ].strip().startswith("/**"):
                    self.start_line -= 1
                    self.comment_lines += 1
            if not self.file.lines[self.start_line - 1].strip():
                self.start_line -= 1
                self.comment_lines += 1

    @property
    def is_used(self):
        if self.name in Function.ignored_func_names:
            return True
        if len(self.callers) == 0:
            return False
        for caller in self.callers:
            if caller.is_used:
                return True
        return False

    @property
    def lines(self):
        return self.end_line - self.start_line - self.comment_lines

    def __repr__(self):
        infos = [f"{self.lines} lines"]
        if self.is_used:
            infos += [f"{len(self.callers)} callers"]
        elif len(self.callers) > 0:
            infos += [f"{len(self.callers)} unused callers"]
        else:
            infos += [f"unused"]
        return f"{self.type} function {self.name} ({', '.join(infos)})"


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
    print(f"\n\n==== {len(File.ignored)} IGNORED ====")
    for name in File.ignored:
        print(name)
    print(f"\n\n==== {len(db.invalid)} INVALID BRANCHES ({len(db.unused)} unused) ====")
    found = []
    for file in db.invalid:
        print_branch(file, found=found)


def print_unused_functions(db):
    print(
        f"\n\n==== {len(db.unused_func)} UNUSED FUNCTIONS ({db.unused_func_lines} lines) ===="
    )
    for file in db.used:
        funcs = file.get_unused_functions()
        if len(funcs) > 0:
            print(file)
            for func in funcs:
                print(" ∟", func)
            print()


def print_specific(db, names):
    print("\n\n==== SPECIFIC CLASSES ====")
    for name in names:
        if name not in db.classes:
            return
        file = db.classes[name]
        print(file)
        func_callers = []
        for func in file.functions:
            print(" ∟", func)
            if len(func.callers) < 5:
                for caller in func.callers:
                    func_callers += [caller]
                    if caller != file:
                        if caller.is_used:
                            print("    ←", caller)
                        else:
                            print("    ↤", caller)
        other_callers = [
            caller for caller in file.callers if caller not in func_callers
        ]
        if len(other_callers) > 0:
            print("other callers:")
            for caller in other_callers:
                if caller.is_used:
                    print(" ←", caller)
                else:
                    print(" ↤", caller)
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
        print(text + "y")
        choice = "y"
    else:
        choice = input(text)
        choice = choice.lower()[0] if choice else "n"
    if choice == "c":
        return choice
    if choice == "y" or choice == "r" or choice == "a":
        to_delete += [file]
        for called in file.called:
            if not called.is_used and called not in to_delete:
                for caller in called.callers:
                    if caller not in to_delete:
                        break
                else:
                    new_choice = remove_file(
                        called,
                        level + 1,
                        to_delete,
                        force or choice == "r" or choice == "a",
                    )
                    if new_choice == "c":
                        return new_choice
    return "a" if choice == "a" else None


def remove_files(db):
    print("\n\n==== REMOVING UNUSED FILES ====")
    to_delete = []
    force = False
    for file in db.invalid:
        choice = remove_file(file, to_delete=to_delete, force=force)
        if choice == "a":
            force = True
        elif choice == "c":
            to_delete = []
            break
    t0 = time()
    for file in to_delete:
        os.unlink(file.filename)
    time_print(t0, f"removed {len(to_delete)} files")


def remove_func(db):
    print("\n\n==== REMOVING UNUSED FUNCTIONS ====")
    to_remove = []
    stop = False
    force = False
    for file in db.used:
        if stop:
            break
        funcs = file.get_unused_functions()
        funcs.sort(key=lambda func: func.start_line, reverse=True)
        if len(funcs) > 0:
            print(file)
        force_file = False
        for func in funcs:
            text = f" ∟ {func.type} function {func.name} ({func.lines} lines) => delete (yes/no/all/file/cancel) (n)? "
            if force or force_file:
                print(text + "y")
                choice = "y"
            else:
                choice = input(text)
                choice = choice.lower()[0] if choice else "n"
            if choice == "y":
                to_remove += [func]
            elif choice == "a":
                to_remove += [func]
                force = True
            elif choice == "f":
                force_file = True
            elif choice == "c":
                stop = True
                to_remove = []
                break
    to_rewrite = []
    for func in to_remove:
        file = func.file
        file.lines = file.lines[: func.start_line] + file.lines[func.end_line + 1 :]
        if file not in to_rewrite:
            to_rewrite += [file]
    print(f"removed {len(to_remove)} functions")
    for file in to_rewrite:
        with open(file.filename, mode="w") as f:
            f.write("\n".join(file.lines))
    print(f"rewrote {len(to_rewrite)} files")


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
    File.ignored_func = File.ignored + config.getlist("input", "ignored_func")
    Function.ignored_func_names = config.getlist("input", "ignored_func_names")

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
        f"scanned classes and found {len(db.invalid)} invalid roots for {len(db.unused)} unused files and {len(db.unused_func)} unused functions ({db.unused_func_lines} lines)",
    )

    if config.get("output", "output_file"):
        write_output(db, config.get("output", "output_file"))

    if config.getboolean("output", "print_invalid"):
        print_invalid_branches(db)

    if config.getboolean("output", "print_functions"):
        print_unused_functions(db)

    if config.getboolean("output", "print_specific"):
        print_specific(db, config.getlist("output", "to_scan"))

    if config.getboolean("output", "remove_files"):
        remove_files(db)

    if config.getboolean("output", "remove_func"):
        remove_func(db)


main()
