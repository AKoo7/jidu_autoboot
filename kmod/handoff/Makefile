obj-m := jidu6801_handoff.o
jidu6801_handoff-y := jidu6801_handoff_main.o go_stub.o
KDIR ?= /home/a/jidu6801/kmod/linux
all:
	cp jidu6801_handoff.c jidu6801_handoff_main.c
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- modules
clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
