/**
  ******************************************************************************
  * @file           : syscalls.c
  * @brief          : This file implements printf functionality
  ******************************************************************************
*/

#include <sys/unistd.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <errno.h>
#include <board.h>

// Minimal syscall stubs. The firmware only does output (via _write, defined in
// communication.cpp) and heap growth (_sbrk, below). These remaining POSIX
// hooks are not supported on bare metal; we define them explicitly so the
// behavior is predictable (fail with ENOSYS) instead of pulling in newlib's
// warning-emitting nosys stubs. Return values mirror libnosys exactly to keep
// stdio behavior unchanged: -1/ENOSYS for everything, _isatty returns 0.

int _close(int file) { (void)file; errno = ENOSYS; return -1; }
int _fstat(int file, struct stat *st) { (void)file; (void)st; errno = ENOSYS; return -1; }
int _isatty(int file) { (void)file; errno = ENOSYS; return 0; }
int _lseek(int file, int ptr, int dir) { (void)file; (void)ptr; (void)dir; errno = ENOSYS; return -1; }
int _read(int file, char *data, int len) { (void)file; (void)data; (void)len; errno = ENOSYS; return -1; }
int _kill(int pid, int sig) { (void)pid; (void)sig; errno = ENOSYS; return -1; }
pid_t _getpid(void) { errno = ENOSYS; return -1; }

extern char _end; // provided by the linker script: it's end of statically allocated section, which is where the heap starts.
extern char _heap_end_max; // provided by the linker script
void* _end_ptr = &_end;
void* _heap_end_max_ptr = &_heap_end_max;
void* heap_end_ptr = 0;

/* @brief Increments the program break (aka heap end)
*
* This is called internally by malloc once it runs out
* of heap space. Malloc might expect a contiguous heap,
* so we don't call the FreeRTOS pvPortMalloc here.
* If this function returns -1, malloc will return NULL.
* Note that if this function returns NULL, malloc does not
* consider this as an error and will return the pointer 0x8.
*
* You should still be careful with using malloc though,
* as it does not guarantee thread safety.
*
* @return A pointer to the newly allocated block on success
*         or -1 otherwise.
*/
intptr_t _sbrk(size_t size) {
    intptr_t ptr;
	{
        uint32_t mask = cpu_enter_critical();
        if (!heap_end_ptr)
            heap_end_ptr = _end_ptr;
        if (heap_end_ptr + size > _heap_end_max_ptr) {
            ptr = -1;
        } else {
            ptr = (intptr_t)heap_end_ptr;
            heap_end_ptr += size;
        }
        cpu_exit_critical(mask);
	}
    return ptr;
}

// _write is defined in communication.cpp


