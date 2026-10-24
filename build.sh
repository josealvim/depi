#!/bin/env bash
# SPDX-FileCopyrightText: 2026 depi contributors
# SPDX-License-Identifier: GPL-3.0-or-later
gcc -O2 -std=c23 -D_POSIX_C_SOURCE=202610 -Wall -Wextra -Werror depi.c -o depi
