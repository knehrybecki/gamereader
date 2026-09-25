#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <spawn.h>
#include <sys/wait.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>

extern char **environ;

static void die(const char *msg) {
    perror(msg);
    exit(1);
}

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;

    char exe_path[PATH_MAX];
    uint32_t size = sizeof(exe_path);
    if (_NSGetExecutablePath(exe_path, &size) != 0) {
        fprintf(stderr, "Could not resolve executable path\n");
        return 1;
    }

    char resolved[PATH_MAX];
    if (realpath(exe_path, resolved) == NULL) {
        strncpy(resolved, exe_path, sizeof(resolved) - 1);
    }

    char *dir = dirname(resolved);
    char resources[PATH_MAX];
    char resources_real[PATH_MAX];
    snprintf(resources, sizeof(resources), "%s/../Resources", dir);
    if (realpath(resources, resources_real) == NULL) {
        fprintf(stderr, "Missing Resources at %s\n", resources);
        return 1;
    }

    char python[PATH_MAX];
    char script[PATH_MAX];
    char venv[PATH_MAX];
    snprintf(python, sizeof(python), "%s/venv/bin/python", resources_real);
    snprintf(script, sizeof(script), "%s/gamereader_gui.py", resources_real);
    snprintf(venv, sizeof(venv), "%s/venv", resources_real);

    if (access(python, X_OK) != 0) {
        fprintf(stderr, "Missing Python runtime at %s\n", python);
        return 1;
    }
    if (access(script, R_OK) != 0) {
        fprintf(stderr, "Missing app script at %s\n", script);
        return 1;
    }

    setenv("PYTHONUNBUFFERED", "1", 1);
    setenv("TK_SILENCE_DEPRECATION", "1", 1);
    setenv("VIRTUAL_ENV", venv, 1);

    pid_t pid = 0;
    char *args[] = {python, script, NULL};
    if (posix_spawn(&pid, python, NULL, NULL, args, environ) != 0) {
        die("posix_spawn");
    }

    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        die("waitpid");
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return 1;
}
