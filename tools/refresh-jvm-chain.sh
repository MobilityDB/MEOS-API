#!/usr/bin/env bash
# refresh-jvm-chain.sh — compatibility shim for refresh-binding.sh.
#
# The JVM consumers (MobilityFlink / MobilitySpark / MobilityKafka) call this through their
# tools/refresh-from-master.sh wrapper. The generic entry point for every binding is now
# tools/refresh-binding.sh; a JVM consumer is just a binding whose tools/refresh.conf sets
# JMEOS_COORDS (which triggers the JMEOS-jar leg). This forwards unchanged — --consumer is an
# accepted alias for --binding — so those wrappers keep working; new bindings call
# refresh-binding.sh directly.
exec "$(dirname "$0")/refresh-binding.sh" "$@"
