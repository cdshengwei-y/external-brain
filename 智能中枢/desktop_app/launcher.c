#include <libgen.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc == 0) {
        return 1;
    }

    char executable[PATH_MAX];
    if (realpath(argv[0], executable) == NULL) {
        return 1;
    }

    char path[PATH_MAX];
    snprintf(path, sizeof(path), "%s", executable);
    for (int index = 0; index < 4; ++index) {
        char *parent = dirname(path);
        if (parent == NULL) {
            return 1;
        }
        snprintf(path, sizeof(path), "%s", parent);
    }

    if (setenv("PYTHONPATH", path, 1) != 0) {
        return 1;
    }

    char script[PATH_MAX];
    int written = snprintf(
        script,
        sizeof(script),
        "%s/desktop_app/upper_computer.py",
        path);
    if (written < 0 || (size_t)written >= sizeof(script)) {
        return 1;
    }

    execl(
        "/usr/bin/python3",
        "/usr/bin/python3",
        script,
        (char *)NULL);
    return 1;
}
