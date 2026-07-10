#include "kernel/calls.h"
#include "kernel/task.h"
#include "kernel/fs.h"
#include "fs/fd.h"
#include "fs/poll.h"
#include "util/sync.h"

// pidfd_open(2): a file descriptor that refers to a process. poll() returns
// POLLIN once the process has terminated (become a zombie / been reaped).
// bun uses pidfd to reap the helper processes it spawns (e.g. the `git` it
// runs at startup); with the old ENOSYS stub it fell back to a path that
// deadlocked its worker pool under iSH.

static struct fd_ops pidfd_ops;

// Registry of live pidfds so process exit can wake any poller waiting on them.
static struct list pidfd_list = LIST_INITIALIZER(pidfd_list);
static lock_t pidfd_lock = LOCK_INITIALIZER;

static bool pid_has_exited(pid_t_ pid) {
    lock(&pids_lock);
    struct task *task = pid_get_task_zombie(pid);
    bool exited = (task == NULL) || task->zombie;
    unlock(&pids_lock);
    return exited;
}

int_t sys_pidfd_open(pid_t_ pid, uint_t flags) {
    STRACE("pidfd_open(%d, %#x)", pid, flags);
    // Only PIDFD_NONBLOCK (== O_NONBLOCK) is defined; iSH pidfds never block on
    // read anyway, so accept it and ignore.
    if (flags & ~(O_NONBLOCK_))
        return _EINVAL;

    lock(&pids_lock);
    struct task *task = pid_get_task(pid);
    if (task == NULL) {
        unlock(&pids_lock);
        return _ESRCH;
    }
    // Linux restricts pidfd_open to thread-group leaders.
    bool is_leader = (task->group != NULL && task->group->leader == task);
    unlock(&pids_lock);
    if (!is_leader)
        return _EINVAL;

    struct fd *fd = adhoc_fd_create(&pidfd_ops);
    if (fd == NULL)
        return _ENOMEM;
    fd->pidfd.pid = pid;
    fd->flags = flags;

    lock(&pidfd_lock);
    list_add(&pidfd_list, &fd->pidfd_links);
    unlock(&pidfd_lock);

    return f_install(fd, flags & O_CLOEXEC_);
}

static int pidfd_poll(struct fd *fd) {
    return pid_has_exited(fd->pidfd.pid) ? POLL_READ : 0;
}

static int pidfd_close(struct fd *fd) {
    lock(&pidfd_lock);
    list_remove(&fd->pidfd_links);
    unlock(&pidfd_lock);
    return 0;
}

// Called from the exit path when `pid` has become a zombie: wake any pidfd
// poller referencing it so a blocked poll()/epoll returns POLLIN.
void pidfd_notify_exit(pid_t_ pid) {
    lock(&pidfd_lock);
    struct fd *fd;
    list_for_each_entry(&pidfd_list, fd, pidfd_links) {
        if (fd->pidfd.pid == pid)
            poll_wakeup(fd, POLL_READ);
    }
    unlock(&pidfd_lock);
}

static struct fd_ops pidfd_ops = {
    .poll = pidfd_poll,
    .close = pidfd_close,
};
