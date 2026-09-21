// JIDU6801 handoff — stage 1+2: own a contiguous staging area, and (on command)
// hand off to our own kernel staged inside it via a firmware CPU_ON.
//
// /proc/jidu6801_stage   : read  -> layout + crc32 of the staging run
//                          write -> "zero"
// /proc/jidu6801_go      : write -> "go <cpu> <image_pa> <dtb_pa> <len>"
//                          flushes staging, sanity-checks the payload, starts
//                          the target core at the stub, then never returns:
//                          this CPU is powered off (or parked if that fails).
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/vermagic.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/mm.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/kallsyms.h>
#include <linux/delay.h>

#define CHUNK_ORDER 10
#define MAX_CHUNKS  24
#define PSCI_CPU_ON_SMC64   0xC4000003UL
#define PSCI_AFFINITY_INFO  0xC4000004UL

extern unsigned char jh_go_stub_start[], jh_go_stub_end[], jh_go_target[];

static void *chunks[MAX_CHUNKS];
static unsigned long chunk_pa[MAX_CHUNKS];
static int nchunks;
static unsigned long run_pa, run_size, run_va;
static void *stub_page;
static int (*psci_cpu_off_fn)(u32 state);

static u32 crc32_le_(u32 crc, const void *p, size_t len)
{
	const u8 *b = p; int i;
	while (len--) { crc ^= *b++; for (i = 0; i < 8; i++) crc = (crc >> 1) ^ (0xEDB88320u & -(crc & 1)); }
	return crc;
}

/* The deployed module powers CPU0 off ("direct CPU_OFF rc=..." is in the shipped
 * .ko), which is the behaviour this file is supposed to build.  A debug build
 * that parks CPU0 in WFI instead was left switched on here once, so the source
 * no longer matched the artifact on the device - a rebuild would have silently
 * changed handoff behaviour.  The variant is now an explicit knob: 0 = deployed
 * behaviour, 1 = park (useful to A/B whether CPU0's power-off is implicated). */
#define JH_PARK_CPU0 0
static long psci_smc(u64 fn, u64 a1, u64 a2, u64 a3)
{
	register u64 x0 asm("x0") = fn;
	register u64 x1 asm("x1") = a1;
	register u64 x2 asm("x2") = a2;
	register u64 x3 asm("x3") = a3;
	asm volatile("smc #0" : "+r"(x0) : "r"(x1), "r"(x2), "r"(x3)
		: "x4","x5","x6","x7","x8","x9","x10","x11","x12","x13","x14",
		  "x15","x16","x17","memory");
	return (long)x0;
}

/* clean+invalidate a VA range to the point of coherency */
static void dc_flush(void *va, unsigned long len, int invalidate)
{
	unsigned long a = (unsigned long)va & ~63UL;
	unsigned long e = ((unsigned long)va + len + 63UL) & ~63UL;
	if (invalidate) {
		for (; a < e; a += 64)
			asm volatile("dc ivac, %0" :: "r"(a) : "memory");
	} else {
		for (; a < e; a += 64)
			asm volatile("dc civac, %0" :: "r"(a) : "memory");
	}
	asm volatile("dsb sy" ::: "memory");
}

static void find_run(void)
{
	int idx[MAX_CHUNKS], i, j, bestlen = 0;
	for (i = 0; i < nchunks; i++) idx[i] = i;
	for (i = 0; i < nchunks; i++)
		for (j = i + 1; j < nchunks; j++)
			if (chunk_pa[idx[j]] < chunk_pa[idx[i]]) { int t = idx[i]; idx[i] = idx[j]; idx[j] = t; }
	for (i = 0; i < nchunks; ) {
		unsigned long start = chunk_pa[idx[i]], len = PAGE_SIZE << CHUNK_ORDER;
		j = i + 1;
		while (j < nchunks && chunk_pa[idx[j]] == start + len) { len += PAGE_SIZE << CHUNK_ORDER; j++; }
		if (len > bestlen) { bestlen = len; run_pa = start; run_size = len; run_va = (unsigned long)chunks[idx[i]]; }
		i = j;
	}
}

static int stage_show(struct seq_file *m, void *v)
{
	int i;
	seq_printf(m, "vermagic   : %s\n", VERMAGIC_STRING);
	seq_printf(m, "chunks     : %d x %lu MiB\n", nchunks, (PAGE_SIZE << CHUNK_ORDER) >> 20);
	seq_printf(m, "run_phys   : 0x%lx\n", run_pa);
	seq_printf(m, "run_size   : 0x%lx (%lu MiB)\n", run_size, run_size >> 20);
	seq_printf(m, "run_va     : 0x%lx\n", run_va);
	seq_printf(m, "cpu_off_fn : %px\n", psci_cpu_off_fn);
	for (i = 0; i < nchunks; i++)
		seq_printf(m, "  chunk[%02d] pa=0x%lx\n", i, chunk_pa[i]);
	if (run_va) {
		const u8 *p = (const u8 *)run_va;
		seq_printf(m, "run_crc32  : 0x%08x\n", ~crc32_le_(~0u, p, run_size));
		seq_printf(m, "head16     : ");
		for (i = 0; i < 16; i++) seq_printf(m, "%02x", p[i]);
		seq_printf(m, "\ntail16     : ");
		for (i = 0; i < 16; i++) seq_printf(m, "%02x", p[run_size - 16 + i]);
		seq_printf(m, "\n");
	}
	return 0;
}
static int stage_open(struct inode *in, struct file *f) { return single_open(f, stage_show, NULL); }
static ssize_t stage_write(struct file *f, const char __user *u, size_t n, loff_t *o)
{
	char c[16];
	if (n > sizeof c - 1) n = sizeof c - 1;
	if (copy_from_user(c, u, n)) return -EFAULT;
	c[n] = 0;
	if (!strncmp(c, "zero", 4) && run_va) memset((void *)run_va, 0, run_size);
	return n;
}
static const struct file_operations stage_fops = {
	.owner = THIS_MODULE, .open = stage_open, .read = seq_read, .write = stage_write,
	.llseek = seq_lseek, .release = single_release,
};

static ssize_t go_write(struct file *f, const char __user *u, size_t n, loff_t *o)
{
	char c[96];
	unsigned long cpu = 0, image_pa = 0, dtb_pa = 0, len = 0;
	long rc;
	u64 *target;

	if (n >= sizeof c) n = sizeof c - 1;
	if (copy_from_user(c, u, n)) return -EFAULT;
	c[n] = 0;
	if (sscanf(c, "go %lu %lx %lx %lx", &cpu, &image_pa, &dtb_pa, &len) != 4)
		return -EINVAL;

	if (!run_pa) { pr_err("JIDU6801-HANDOFF: no staging\n"); return -ENOMEM; }

	/* refuse to jump outside memory we own */
	if (image_pa < run_pa || image_pa + len > run_pa + run_size ||
	    dtb_pa < run_pa || dtb_pa + 64 > run_pa + run_size) {
		pr_err("JIDU6801-HANDOFF: payload outside staging (img 0x%lx dtb 0x%lx run 0x%lx+0x%lx)\n",
		       image_pa, dtb_pa, run_pa, run_size);
		return -ERANGE;
	}
	/* sanity: arm64 Image header magic at +56, DTB magic at +0 */
	{
		const u8 *img = (const u8 *)(run_va + (image_pa - run_pa));
		const u8 *dtb = (const u8 *)(run_va + (dtb_pa - run_pa));
		dc_flush((void *)dtb, 64, 1);
		if (img[56] != 'A' || img[57] != 'R' || img[58] != 'M' || img[59] != 'd') {
			pr_err("JIDU6801-HANDOFF: no arm64 Image magic at 0x%lx\n", image_pa);
			return -EINVAL;
		}
		if (!(dtb[0] == 0xd0 && dtb[1] == 0x0d && dtb[2] == 0xfe && dtb[3] == 0xed)) {
			pr_err("JIDU6801-HANDOFF: no DTB magic at 0x%lx\n", dtb_pa);
			return -EINVAL;
		}
	}

	/* target core must really be off */
	rc = psci_smc(PSCI_AFFINITY_INFO, 0x80000000UL | cpu, 0, 0);
	if (rc != 1) {
		pr_err("JIDU6801-HANDOFF: cpu%lu not OFF (affinity_info=%ld) - refusing\n", cpu, rc);
		return -EBUSY;
	}

	/* The target core starts with caches off, so push the payload to DRAM.
	 * Then clean+invalidate cached RAM so this CPU's dirty lines can never be
	 * written back over the new kernel's memory later.
	 *
	 * Only linear-mapped RAM may be touched: every region the DTB marks
	 * "no-map" (ramoops, secmon, and the wmcpu/wo firmware buffers) has NO
	 * linear mapping, and secmon is additionally secure - a dc on it aborts
	 * and panics the kernel (learned the hard way).  Ranges below are the
	 * DT's reserved-memory nodes subtracted from DRAM [0x40000000,0x60000000),
	 * which also keeps us out of the WiFi MCU's shared buffers. */
	{
		unsigned long delta = run_va - run_pa;
		pr_emerg("JIDU6801-HANDOFF: flushing payload (0x%lx+0x%lx), then cached RAM\n",
			 run_va, len);
		dc_flush((void *)run_va, len, 0);
		dc_flush((void *)(delta + 0x40000000UL), 0x2ff0000UL, 0);   /* 4000_0000..42ff_0000 */
		dc_flush((void *)(delta + 0x43040000UL), 0xcbc0000UL, 0);   /* 4303_0000..4fc0_0000 */
		dc_flush((void *)(delta + 0x4ffc0000UL), 0x10040000UL, 0);  /* 4ffc_0000..6000_0000 */
		pr_emerg("JIDU6801-HANDOFF: cache clean+invalidate done\n");
	}
	memcpy(stub_page, jh_go_stub_start, jh_go_stub_end - jh_go_stub_start);
	target = (u64 *)((unsigned long)stub_page + (jh_go_target - jh_go_stub_start));
	*target = image_pa;
	dc_flush(stub_page, 4096, 0);

	pr_emerg("JIDU6801-HANDOFF: image=0x%lx dtb=0x%lx len=0x%lx -> cpu%lu, stub=0x%lx\n",
		 image_pa, dtb_pa, len, cpu, (unsigned long)virt_to_phys(stub_page));

	rc = psci_smc(PSCI_CPU_ON_SMC64, cpu, virt_to_phys(stub_page), dtb_pa);
	if (rc) {
		pr_err("JIDU6801-HANDOFF: CPU_ON cpu%lu failed rc=%ld\n", cpu, rc);
		return -EIO;
	}
	pr_emerg("JIDU6801-HANDOFF: CPU_ON accepted, cpu%lu is starting our kernel; parking cpu0\n", cpu);

	/* Never return: this CPU's Linux state is about to be invalidated. */
	local_irq_disable();
	if (JH_PARK_CPU0) { pr_emerg("JIDU6801-HANDOFF: [park variant] skipping CPU_OFF, wfi park only\n"); for (;;) asm volatile("wfi"); }
	if (psci_cpu_off_fn) {
		psci_cpu_off_fn(0x10000);	/* Linux's own path, if resolvable */
	} else {
		/* psci_ops is a non-exported data symbol (absent from kallsyms
		 * without KALLSYMS_ALL), so drive the SMC ourselves: SMC32 id
		 * 0x84000002 with the POWER_DOWN state in w1 - the exact call
		 * Linux makes and the only form this BL31 accepts. */
		rc = psci_smc(0x84000002UL, 0x10000, 0, 0);
		pr_emerg("JIDU6801-HANDOFF: direct CPU_OFF rc=%ld\n", rc);
	}
	pr_emerg("JIDU6801-HANDOFF: cpu_off did not take, parking in wfi\n");
	for (;;)
		asm volatile("wfi");
	return n;
}
static const struct file_operations go_fops = {
	.owner = THIS_MODULE, .write = go_write, .llseek = seq_lseek,
};

static int __init h_init(void)
{
	int i;
	for (i = 0; i < MAX_CHUNKS; i++) {
		void *p = (void *)__get_free_pages(GFP_KERNEL | __GFP_ZERO, CHUNK_ORDER);
		if (!p) break;
		chunks[i] = p; chunk_pa[i] = virt_to_phys(p);
	}
	nchunks = i;
	if (!nchunks) { pr_err("JIDU6801-HANDOFF: no staging memory\n"); return -ENOMEM; }
	find_run();
	stub_page = (void *)__get_free_page(GFP_KERNEL | __GFP_ZERO);
	{
		struct { void *gv, *sus, *off, *on, *mig, *aff, *mit; int conduit, smccc; } *o;
		o = (void *)kallsyms_lookup_name("psci_ops");
		if (o) psci_cpu_off_fn = o->off;
	}
	pr_info("JIDU6801-HANDOFF: staging pa=0x%lx size=0x%lx va=0x%lx; cpu_off=%px stub_pa=0x%lx\n",
		run_pa, run_size, run_va, psci_cpu_off_fn,
		stub_page ? (unsigned long)virt_to_phys(stub_page) : 0);
	proc_create("jidu6801_stage", 0644, NULL, &stage_fops);
	proc_create("jidu6801_go", 0200, NULL, &go_fops);
	return 0;
}
static void __exit h_exit(void)
{
	int i;
	remove_proc_entry("jidu6801_stage", NULL);
	remove_proc_entry("jidu6801_go", NULL);
	if (stub_page) free_page((unsigned long)stub_page);
	for (i = 0; i < nchunks; i++) free_pages((unsigned long)chunks[i], CHUNK_ORDER);
	pr_info("JIDU6801-HANDOFF: released %d chunks\n", nchunks);
}
module_init(h_init);
module_exit(h_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("JIDU6801 staging + kernel handoff");
