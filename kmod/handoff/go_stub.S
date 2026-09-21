/* Handoff stub: entered by firmware CPU_ON with MMU + D-cache off, x0 = context_id.
 * Puts the machine in the state the arm64 boot protocol demands (interrupts
 * masked, x1..x3 zero) and branches to the staged kernel Image carried in the
 * patched literal below.  x0 already holds the DTB physical address.
 */
	.section .rodata
	.balign 64
	.globl jh_go_stub_start
	.globl jh_go_stub_end
	.globl jh_go_target
jh_go_stub_start:
	msr	daifset, #0xf		/* boot protocol: all interrupts masked */
	mov	x1, xzr
	mov	x2, xzr
	mov	x3, xzr
	ldr	x4, jh_go_target	/* patched by the module: kernel Image pa */
	br	x4
	.balign 8
jh_go_target:
	.quad	0
jh_go_stub_end:
