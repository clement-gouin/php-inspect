import os
import re
from time import time
from configparser import ConfigParser

COLORS = [
    "087",  # #5fffff
    "085",  # #5fffaf
    "155",  # #afff5f
    "227",  # #ffff5f
    "215",  # #ffaf5f
    "205",  # #ff5faf
    "207",  # #ff5fff
    "135",  # #af5fff
    "075",  # #5fafff
]

KEYWORD_COLOR = "111"  # #87afff
NAME_COLOR = "228"  # #ffff87
NUMBER_COLOR = "156"  # #afff87

KEYWORDS = [
    "abstract",
    "branches",
    "callers",
    "class",
    "entrypoint",
    "extends",
    "function",
    "functions",
    "interface",
    "lines",
    "public",
    "private",
    "protected",
    "static",
    "trait",
    "unknown",
    "unused",
    "reflexive",
]


def colorize(text, color=None):
    if color is None:
        return auto_colorize(str(text))
    return f"\033[38;5;{color}m{str(text)}\033[0m"


def colorize_namespace(ns):
    return "\\".join(
        colorize(fragment, COLORS[i % len(COLORS)])
        for i, fragment in enumerate(ns.split("\\"))
    )


def auto_colorize(text):
    if " " in text:
        return " ".join(auto_colorize(fragment) for fragment in text.split(" "))
    if text in KEYWORDS:
        return colorize(text, KEYWORD_COLOR)
    elif text.isdigit():
        return colorize(text, NUMBER_COLOR)
    elif text.isalnum():
        return colorize(text, NAME_COLOR)
    elif "\\" in text or "/" in text:
        return colorize_namespace(text)
    else:
        return text


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


def backward_comment(lines, start_line):
    comment_lines = 0
    deprecated = False
    if start_line > 0 and lines[start_line - 1].strip().endswith("*/"):
        while start_line >= 0 and not lines[start_line].strip().startswith("/**"):
            start_line -= 1
            comment_lines += 1
            if start_line >= 0 and "@deprecated" in lines[start_line].lower():
                deprecated = True
    if start_line > 0 and not lines[start_line - 1].strip():
        start_line -= 1
        comment_lines += 1
    return start_line, comment_lines, deprecated


class DB:
    def __init__(self, root_path, entrypoint_paths):
        self.root_path = os.path.realpath(root_path)
        self.entrypoint_paths = [
            os.path.realpath(os.path.join(root_path, path)) for path in entrypoint_paths
        ]
        self.files = []
        self.entrypoints = []
        self.classes = {}
        self.invalid_roots = []
        self.invalid_roots_deprecated = []
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

        self.invalid_roots = [
            file for file in self.classes.values() if file.is_invalid_root(False)
        ]
        self.invalid_roots_deprecated = [
            file for file in self.classes.values() if file.is_invalid_root(True)
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
        self.deprecated = False
        self.class_start_line = None
        self.class_comment_lines = None
        self._imports = None
        self._is_used = None
        self.parent = None
        self.reflexive_call = False

    def load(self, root_path):
        with open(os.path.join(root_path, self.filename)) as f:
            self.content = f.read()
        if not self.is_entrypoint:
            self.lines = self.content.split("\n")
            for i, line in enumerate(self.lines):
                line = line.strip()
                match_namespace = re.match(r"^namespace (App\\[\\\w]+);$", line)
                match_import = re.match(r"^use (App\\[\\\w]+)( as (\w+))?;$", line)
                match_class = re.match(
                    r"^(abstract\s+)?(class|interface|trait)\s+(\w+)(\s+extends\s+(\w+))?",
                    line,
                )
                match_function = re.match(
                    r"^(abstract\s+)?(public|protected|private)\s+(static\s+)?function\s+(\w+)",
                    line,
                )
                match_reflexive = re.match(
                    r".*\$this->({\$|\$\w+\()",
                    line,
                )
                if match_namespace and self.namespace is None:
                    self.namespace = match_namespace.groups()[0]
                if match_import:
                    self.raw_imports += [match_import.groups()[0]]
                    if match_import.groups()[2]:
                        self.alias_imports[
                            self.raw_imports[-1]
                        ] = match_import.groups()[2]
                if match_class and self.classname is None:
                    self.class_line = i
                    (
                        self.class_start_line,
                        self.class_comment_lines,
                        self.deprecated,
                    ) = backward_comment(self.lines, i)
                    self.classname = match_class.groups()[2]
                    self.type = match_class.groups()[1]
                    if match_class.groups()[4] is not None:
                        self.parent = match_class.groups()[4]
                if match_function and not match_function.groups()[3].startswith("__"):
                    func = Function(
                        self, match_function.groups()[1], match_function.groups()[3], i
                    )
                    func.load()
                    self.functions += [func]
                if match_reflexive:
                    self.reflexive_call = True
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
                                and not other_func.call_other_same
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
        elif self.deprecated:
            return False
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

    def is_invalid_root(self, exclude_deprecated):
        if self.is_used or self.deprecated and exclude_deprecated:
            return False
        if exclude_deprecated:
            return (
                len([caller for caller in self.callers if not caller.deprecated]) == 0
            )
        else:
            return len(self.callers) == 0

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
            infos = [colorize(f"{len(self.functions)} functions")]
            if self.is_used:
                infos += [colorize(f"{len(self.callers)} callers")]
            elif len(self.callers) > 0:
                infos += [colorize(f"{len(self.callers)} unused callers")]
            else:
                infos += [colorize(f"unused")]
            if self.parent is not None:
                infos += [colorize(f"extends {self.parent}")]
            return colorize(f"{self.type} {self.full_classname} ({', '.join(infos)})")
        elif self.is_entrypoint:
            return colorize(f"entrypoint - {self.filename}")
        else:
            return colorize(f"unknown - {self.filename}")


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
        self.call_other_same = False
        self.deprecated = False

    def load(self):
        if self.file.lines[self.start_line].strip().endswith(";"):
            self.end_line = self.start_line
        else:
            state = 0
            found = False
            name_count = 0
            for i, line in enumerate(self.file.lines[self.start_line :]):
                state += line.count("{") - line.count("}")
                found = found or line.count("{") > 0
                name_count += line.lower().count(self.name.lower())
                if state == 0 and found:
                    self.end_line = self.start_line + i
                    break
            self.call_other_same = name_count > 1
            self.start_line, self.comment_lines, self.deprecated = backward_comment(
                self.file.lines, self.start_line
            )

    @property
    def is_used(self):
        if self.name in Function.ignored_func_names:
            return True
        if self.deprecated:
            return False
        if self.file.reflexive_call:
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
        infos = [colorize(f"{self.lines} lines")]
        if self.is_used:
            if len(self.callers) == 0:
                infos += [colorize(f"reflexive")]
            else:
                infos += [colorize(f"{len(self.callers)} callers")]
        elif len(self.callers) > 0:
            infos += [colorize(f"{len(self.callers)} unused callers")]
        else:
            infos += [colorize(f"unused")]
        return colorize(f"{self.type} function {self.name} ({', '.join(infos)})")


def time_print(t0, message):
    print(f"({1000*(time()-t0):.1f}ms) {message}")


def print_branch(file, print_deprecated, level=0, found=[]):
    if file.deprecated and not print_deprecated:
        return
    if level == 0:
        print(colorize_namespace(file.full_classname))
    else:
        print((level - 1) * 2 * " ", "∟", colorize_namespace(file.full_classname))
    found += [file]
    for called in file.called:
        if not called.is_used and called not in found:
            print_branch(called, print_deprecated, level + 1, found)


def print_invalid_branches(db, print_deprecated):
    roots = db.invalid_roots if print_deprecated else db.invalid_roots_deprecated
    print(f"\n\n==== {len(File.ignored)} IGNORED ====")
    for name in File.ignored:
        print(colorize_namespace(name))
    print(f"\n\n==== {len(roots)} INVALID BRANCHES ({len(db.unused)} unused) ====")
    found = []
    for file in roots:
        print_branch(file, print_deprecated, found=found)


def print_unused_functions(db, print_deprecated):
    funcs = [func for func in db.unused_func if print_deprecated or not func.deprecated]
    lines = sum([func.lines for func in funcs])
    print(f"\n\n==== {len(funcs)} UNUSED FUNCTIONS ({lines} lines) ====")
    for file in db.used:
        funcs = [
            func
            for func in file.get_unused_functions()
            if print_deprecated or not func.deprecated
        ]
        if len(funcs) > 0:
            print(file)
            for func in funcs:
                print(" ∟", func)
            print()


def print_specific(db, names):
    print("\n\n==== SPECIFIC CLASSES ====")
    for name in names:
        if name not in db.classes:
            print("not found:", name)
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
        text = f"{colorize_namespace(file.full_classname)} => delete (yes/no/all/cancel/recursive) (n)? "
    else:
        text = f"{(level - 1) * 2 * ' ' }∟ {colorize_namespace(file.full_classname)} => delete (yes/no/all/cancel/recursive) (n)? "
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
    for file in db.invalid_roots:
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
            text = f" ∟ {colorize(func.type, KEYWORD_COLOR)} {colorize('function', KEYWORD_COLOR)} {colorize(func.name, NAME_COLOR)} ({func.lines} lines) => delete (yes/no/all/file/cancel) (n)? "
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
        f"scanned classes and found {len(db.invalid_roots)} invalid roots for {len(db.unused)} unused files and {len(db.unused_func)} unused functions ({db.unused_func_lines} lines)",
    )

    if config.get("output", "output_file"):
        write_output(db, config.get("output", "output_file"))

    print_deprecated = config.getboolean("output", "print_deprecated")

    if config.getboolean("output", "print_invalid"):
        print_invalid_branches(db, print_deprecated)

    if config.getboolean("output", "print_functions"):
        print_unused_functions(db, print_deprecated)

    if config.getboolean("output", "print_specific"):
        print_specific(db, config.getlist("output", "to_scan"))

    if config.getboolean("output", "remove_files"):
        remove_files(db)

    if config.getboolean("output", "remove_func"):
        remove_func(db)


main()
