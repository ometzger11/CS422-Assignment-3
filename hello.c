// SPDX-License-Identifier: GPL-2.0
/*
 * hello.c - minimal Hello World kernel module
 */
#include <linux/init.h>
#include <linux/module.h>
#include <linux/printk.h>

static int __init hello_init(void)
{
	pr_info("hello: module loaded\n");
	return 0;
}

static void __exit hello_exit(void)
{
	pr_info("hello: module unloaded\n");
}

module_init(hello_init);
module_exit(hello_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Faisal, Owen, and Trevor");
MODULE_DESCRIPTION("Our congestion control algo for CS 422 Assignment 3");
