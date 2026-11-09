#!/bin/sh
# Runs once while the image is built (docs/spark.md, "Supply chain"). PySpark 4.2.1 ships Java
# libraries with known HIGH/CRITICAL vulnerabilities (Trivy: 62 findings; PySpark 4.3.0 has the
# same versions). This:
#   1. swaps each vulnerable standalone jar for the fixed PATCH release of the same line (same
#      API), checked against Maven Central's SHA-1 before it is used
#   2. removes jars this job never loads: the Spark Connect REPL, the Hive metastore's Derby
#      database and Thrift, and the interactive shell's JLine
# What is left are copies baked inside Hadoop's and Spark's own jars, which only a new Spark
# release can fix: waived one by one, with reasons, in .trivyignore.yaml.
# `make spark-smoke` and the parity run check the job still works with the swapped jars.
set -eu
J=$(python -c "import os, pyspark; print(os.path.join(os.path.dirname(pyspark.__file__), 'jars'))")
M=https://repo1.maven.org/maven2

get() {   # get <group> <artifact> <version> [classifier]: download into the jars folder, verified
  path=$(echo "$1" | tr . /)
  file="$2-$3${4:+-$4}.jar"
  curl -fsSL -o "$J/$file" "$M/$path/$2/$3/$file"
  echo "$(curl -fsSL "$M/$path/$2/$3/$file.sha1" | cut -c1-40)  $J/$file" | sha1sum -c --quiet -
  echo "  + $file"
}

swap() {   # swap <old file> <group> <artifact> <version> [classifier]
  [ -f "$J/$1" ] || { echo "expected $1 in PySpark's jars: check the versions here" >&2; exit 1; }
  rm "$J/$1"
  echo "  - $1"
  shift
  get "$@"
}

echo "netty 4.2.13 -> 4.2.17 (CVE-2026-75595 CRITICAL and 12 HIGH): every module, so they match"
for old in "$J"/netty-*-4.2.13.Final*.jar; do
  base=${old##*/}
  name=${base%%-4.2.13.Final*}
  classifier=${base#*-4.2.13.Final}; classifier=${classifier#-}; classifier=${classifier%.jar}
  swap "$base" io.netty "$name" 4.2.17.Final "$classifier"
done

echo "jackson 2.21.2 -> 2.21.7, guava 33.6.0 -> 33.7.2, lz4 1.11.0 -> 1.11.4, snappy 1.1.10.8 -> 1.1.10.9"
swap jackson-core-2.21.2.jar com.fasterxml.jackson.core jackson-core 2.21.7
swap jackson-databind-2.21.2.jar com.fasterxml.jackson.core jackson-databind 2.21.7
swap guava-33.6.0-jre.jar com.google.guava guava 33.7.2-jre
swap lz4-java-1.11.0.jar at.yawk.lz4 lz4-java 1.11.4
swap snappy-java-1.1.10.8.jar org.xerial.snappy snappy-java 1.1.10.9

echo "removed, never loaded by this job:"
for f in derby-10.16.1.1.jar derbyshared-10.16.1.1.jar derbytools-10.16.1.1.jar \
         libthrift-0.16.0.jar jline-3.29.0-jdk8.jar; do
  rm "$J/$f" && echo "  - $f"
done
rm -r "$J/connect-repl" && echo "  - connect-repl/ (the Spark Connect shell)"
