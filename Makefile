obj-m += hello.o
obj-m += tcp_dart.o

KDIR ?= /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
	rm -f tests/dart_sim

# Userspace simulation of tcp_dart.c against a mock kernel API (no root)
sim: tests/dart_sim
	./tests/dart_sim

tests/dart_sim: tests/dart_sim.c tcp_dart.c $(wildcard tests/shim/*/*.h)
	$(CC) -std=gnu11 -O2 -Wall -Wextra -Wno-unused-parameter -Werror \
		-Itests/shim -o $@ tests/dart_sim.c

.PHONY: all clean sim
