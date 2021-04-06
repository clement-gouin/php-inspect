# php-inspect

Inspect PHP code and determine what is unused

Sample `config.ini`:

```ini
[input]
root_path   = /path/to/project/app
entrypoints =
    ../config
    ../bootstrap
    ../routes
ignored     =
    App\Services\Service1
    App\Managers\Manager1

[output]
output_file     = unused.txt
print_invalid   = true
print_functions = true
print_specific  = true
to_scan         =
    App\Services\Service2
    App\Http\Controllers\Controller1
remove_files    = false
```

Sample output:

```
$ python3 php-inspect.py

(9.8ms) found 907 files with 50 entrypoint files
(139.5ms) loaded 852 classes
(5262.5ms) scanned classes and found 47 invalid roots for 73 unused files and 147 unused functions


====IGNORED====
App\Services\Service1
App\Managers\Manager1


====INVALID BRANCHES====
App\Console\Commands\Command1
 ∟ App\Jobs\Job1
   ∟ App\Exceptions\Command1Exception
...


====UNUSED FUNCTIONS====
class App\Services\Service2 (2 functions, 3 callers)
-> protected function getlientList (0 callers)
...


====SPECIFIC CLASSES====
class App\Services\Service2 (2 functions, 3 callers)
callers:
x> App\Jobs\Job1
-> App\Http\Controllers\Controller1
-> App\Http\Controllers\Controller2
...

(0.2ms) wrote 73 lines in unused.txt
```
