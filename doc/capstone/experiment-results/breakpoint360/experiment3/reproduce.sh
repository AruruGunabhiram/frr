#!/usr/bin/env bash
# SPDX-License-Identifier: ISC
# Experiment 3: BGP advertisement-interval (MRAI) 0 / 1 / 5 s on the 5x2 fabric.
# Workload = Experiment 2 staggered 10 s schedule (64 robots, seeded, BBM,
# DAD off, default sender-AS-path behaviour). Two announce profiles:
#   repeat     Arm A, operational: archived 4 gateway pings over 0.75 s
#   keepalive  Arm B, isolated: 4 Hz gateway pings for 24 s (exp3_schedule.py)
set -eu
rel=doc/capstone/experiment-results/breakpoint360/experiment3
exp2=doc/capstone/experiment-results/breakpoint360/experiment2
intervals=(0 1 5)
arms=(keepalive repeat)

container_trial() (
    set -eu
    unset MOB_DIAG
    export MOB_OUT="$1" MOB_ADV_INTERVAL="$2" MOB_DURATION="$((10 * $3))" MOB_SEED="$4"
    export MOB_DWELL=10 MOB_SYNC=0 MOB_ROBOTS=64 MOB_ROBOT="${5:-repeat}" MOB_GAP_MS=0 MOB_DAD=0
    export MOB_COUNT=1 MOB_SUBNET=narrow MOB_PROBE_HZ=10 ENABLE_LIVE_ROUTE_GRAPH=false
    export EXP3_COOLDOWN_S=20 PYTHONDONTWRITEBYTECODE=1
    local temp status=0
    temp=$(mktemp -d)
    trap 'rm -r -- "$temp"' EXIT
    tar -xzf ../../team-handoff/guna/mobility_tools.tar.gz -C "$temp" \
        mobility_scale.py mobility_prober.py route_counter.py bgp_pcap.py \
        test_mobility_scale.py analyze_mobility.py
    ln -s "$PWD/bgp_evpn_capstone/test_evpn_capstone.py" "$temp/"
    # Stage the plugins and analysis helpers too, so a branch checkout in the
    # shared working tree during a trial cannot remove them mid-run.
    cp "$EXP2_ROOT/exp2_schedule.py" "$EXP2_ROOT/summarize.py" \
        "$EXP3_ROOT/exp3_schedule.py" "$EXP3_ROOT/analyze_exp3.py" "$temp/"
    export PYTHONPATH="$temp:$PWD"
    cat /sys/fs/cgroup/memory.events
    pytest -c pytest.ini -p conftest -p exp2_schedule -p exp3_schedule -qx --tb=short \
        "$temp/test_mobility_scale.py" \
        -k "test_fabric_baseline or test_mobility_experiment" || status=$?
    cat /sys/fs/cgroup/memory.events /proc/pressure/memory
    if [[ "$status" != 0 ]]; then exit "$status"; fi
    python3 "$temp/analyze_exp3.py" point "$MOB_OUT"
    date -u +%FT%TZ > "$MOB_OUT/ACCEPTED"
)

container_matrix() {
    # Seed-major, arms interleaved. Trials with an ACCEPTED marker are kept
    # (resume). A trial that fails acceptance is moved to rejected/ (kept)
    # and rerun on a fresh topology, up to 3 attempts.
    local arm adv seed out attempt failures=0
    exec >> "$EXP3_ROOT/trials/matrix.log" 2>&1
    echo "$(date -u +%FT%TZ) Matrix run begins"
    trap 'printf "%s\n" "$?" > "$EXP3_ROOT/trials/matrix.exit"' EXIT
    for seed in 1 2 3; do
        for arm in "${arms[@]}"; do
            for adv in "${intervals[@]}"; do
                out="$EXP3_ROOT/trials/$arm/adv-${adv}s/seed-$seed"
                if [[ -f "$out/ACCEPTED" ]]; then
                    echo "Keeping accepted $out"
                    continue
                fi
                for attempt in 1 2 3; do
                    mkdir -p "$out"
                    echo "$(date -u +%FT%TZ) Starting $arm MRAI ${adv}s seed $seed attempt $attempt"
                    if bash "$EXP3_ROOT/reproduce.sh" _trial "$out" "$adv" 6 "$seed" "$arm" \
                            > "$out/pytest.log" 2>&1; then
                        echo "$(date -u +%FT%TZ) Validated $out"
                        break
                    fi
                    echo "$(date -u +%FT%TZ) Failed $out"
                    mkdir -p "$EXP3_ROOT/rejected/$arm/adv-${adv}s"
                    mv "$out" "$EXP3_ROOT/rejected/$arm/adv-${adv}s/seed-$seed-$(date -u +%Y%m%dT%H%M%SZ)"
                    if [[ "$attempt" == 3 ]]; then failures=$((failures + 1)); fi
                done
            done
        done
    done
    (( failures == 0 ))
}

container_reanalyze() (
    # Re-run point analysis on finished trials (no new trials are started).
    set -eu
    local temp point failures=0
    temp=$(mktemp -d)
    trap 'rm -r -- "$temp"' EXIT
    tar -xzf ../../team-handoff/guna/mobility_tools.tar.gz -C "$temp" \
        route_counter.py bgp_pcap.py analyze_mobility.py
    export PYTHONPATH="$temp:$EXP3_ROOT:$EXP2_ROOT" PYTHONDONTWRITEBYTECODE=1
    for point in "$1"/adv-*s/seed-* "$1"/*/adv-*s/seed-*; do
        [[ -d "$point" ]] || continue
        echo "Reanalyzing $point"
        python3 "$EXP3_ROOT/analyze_exp3.py" point "$point" > "$point/analysis.log" 2>&1 ||
            { echo "Acceptance failed: $point"; failures=$((failures + 1)); }
    done
    (( failures == 0 ))
)

case "${1:-}" in
    _trial) shift; container_trial "$@"; exit ;;
    _reanalyze) shift; container_reanalyze "$@"; exit ;;
    _matrix) container_matrix; exit ;;
esac

root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
mount=${FRR_MOUNT:-/home/frr/frr}
container=${FRR_CONTAINER:-frr-dev}
env=(-e "EXP3_ROOT=$mount/$rel" -e "EXP2_ROOT=$mount/$exp2")

run() {
    local group=$1 adv=$2 rounds=$3 seed=$4 arm=${5:-repeat} out
    out="$rel/$group/adv-${adv}s/seed-$seed"
    mkdir -p "$(dirname "$root/$out")"
    mkdir "$root/$out"
    echo "Starting $group MRAI ${adv}s, rounds $rounds, seed $seed"
    docker exec -u root -w "$mount/tests/topotests" "${env[@]}" "$container" \
        bash "$mount/$rel/reproduce.sh" _trial "$mount/$out" "$adv" "$rounds" "$seed" "$arm" \
        > "$root/$out/pytest.log" 2>&1
    echo "Validated $out"
}

case "${1:-}" in
    smoke)
        # Two rounds (128 moves) per interval, seed 1: the second round
        # exercises the censored class if convergence outlasts the dwell.
        failures=0
        for adv in "${intervals[@]}"; do
            run smoke "$adv" 2 1 || { echo "Failed smoke MRAI ${adv}s"; failures=$((failures + 1)); }
        done
        python3 "$root/$rel/analyze_exp3.py" collect "$root/$rel/smoke"
        (( failures == 0 ))
        ;;
    trial)
        # trial ARM ADV SEED, e.g. trial keepalive 5 2
        [[ "${2:-}" =~ ^(repeat|keepalive)$ && "${3:-}" =~ ^(0|1|5)$ && "${4:-}" =~ ^[0-9]+$ ]] || exit 2
        run "trials/$2" "$3" 6 "$4" "$2"
        ;;
    diag)
        # diag ARM ADV SEED: one diagnostic trial outside the matrix. Output
        # goes to diag/, never into trials/. (The 08:54Z capture-drop diagnosis
        # used the socket statistics without the kernel filter.)
        [[ "${2:-}" =~ ^(repeat|keepalive)$ && "${3:-}" =~ ^(0|1|5)$ && "${4:-}" =~ ^[0-9]+$ ]] || exit 2
        run "diag/$2-$(date -u +%Y%m%dT%H%M%SZ)" "$3" 6 "$4" "$2"
        ;;
    matrix-all)
        mkdir "$root/$rel/trials"
        docker exec -d -u root -w "$mount/tests/topotests" "${env[@]}" "$container" \
            bash "$mount/$rel/reproduce.sh" _matrix
        echo "Colima matrix started (18 trials). Check $rel/trials/matrix.log and matrix.exit."
        ;;
    matrix-resume)
        if [[ -f "$root/$rel/trials/matrix.exit" ]]; then
            mv "$root/$rel/trials/matrix.exit" \
                "$root/$rel/trials/matrix.exit.$(date -u +%Y%m%dT%H%M%SZ)"
        fi
        docker exec -d -u root -w "$mount/tests/topotests" "${env[@]}" "$container" \
            bash "$mount/$rel/reproduce.sh" _matrix
        echo "Colima matrix resumed. Accepted trials are kept."
        ;;
    aggregate)
        "${PYTHON:-python3}" "$root/$rel/aggregate_exp3.py"
        ;;
    collect)
        python3 "$root/$rel/analyze_exp3.py" collect "$root/$rel/${2:-trials}"
        ;;
    reanalyze)
        group=${2:-smoke}
        status=0
        docker exec -u root -w "$mount/tests/topotests" "${env[@]}" "$container" \
            bash "$mount/$rel/reproduce.sh" _reanalyze "$mount/$rel/$group" || status=$?
        python3 "$root/$rel/analyze_exp3.py" collect "$root/$rel/$group"
        exit "$status"
        ;;
    *) echo "Usage: $0 {smoke|trial ARM ADV SEED|matrix-all|matrix-resume|aggregate|collect [GROUP]|reanalyze [GROUP]}" >&2; exit 2 ;;
esac
