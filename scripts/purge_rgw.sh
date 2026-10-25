#!/usr/bin/env bash
# ==============================================================================
# RadosGW phases of purge_orphans.sh (source-only)
# ==============================================================================
# Split out of purge_orphans.sh to keep it under the file-size limit; it runs
# in purge_orphans.sh's shell and uses its globals (log, FORCE,
# MAX_DELETE_THRESHOLD, TOTAL_*, _SNAP_RGW_*, _RGW_*), plus rgw_helpers.sh.
#
#   purge_rgw         Phase 1: classify orphaned RGW users into the snapshot.
#   purge_rgw_delete  Phase 2: one DELETE per snapshotted user, with honest
#                     accounting (re-inventory, failure/timeout settlement).

purge_rgw() {
    if ! rgw_available; then
        log "RGW users: skipped (RGW admin credentials not configured)"
        return
    fi

    local orphans_file
    orphans_file=$(mktemp)
    if ! rgw_find_orphaned_users > "${orphans_file}"; then
        _RGW_LISTING_ERRORS=$((_RGW_LISTING_ERRORS + (RGW_LAST_FIND_ERRORS > 0 ? RGW_LAST_FIND_ERRORS : 1)))
        log "RGW users: WARNING — orphan scan failed; RGW purge is disabled for this run"
        rm -f "${orphans_file}"
        return
    fi

    _RGW_LISTING_ERRORS=$((_RGW_LISTING_ERRORS + RGW_LAST_FIND_ERRORS))

    local listed_any=0
    local uid bucket_json bucket_count object_count ownership bucket_name
    while IFS= read -r uid; do
        [[ -n "${uid}" ]] || continue
        listed_any=1
        ownership=$(rgw_classify_owner "${uid}")

        if ! bucket_json=$(rgw_list_user_buckets "${uid}"); then
            _RGW_LISTING_ERRORS=$((_RGW_LISTING_ERRORS + 1))
            log "RGW user ${uid}: WARNING — bucket listing failed; skipping from purge snapshot"
            continue
        fi

        bucket_count=$(rgw_count_buckets "${bucket_json}")
        object_count=$(rgw_count_objects "${bucket_json}")

        if [[ "${ownership}" == "rally_owned" ]]; then
            log "RGW user ${uid}: orphaned (rally-owned, ${bucket_count} buckets, ${object_count} objects)"
            TOTAL_FOUND=$((TOTAL_FOUND + 1))
            _SNAP_RGW_USERS+=("${uid}")
            _RGW_PURGE_DATA["${uid}"]=true
            while IFS= read -r bucket_name; do
                [[ -n "${bucket_name}" ]] || continue
                log "  RGW bucket ${bucket_name}"
                TOTAL_FOUND=$((TOTAL_FOUND + 1))
                _SNAP_RGW_BUCKETS+=("${uid}|${bucket_name}")
            done < <(echo "${bucket_json}" | jq -r '.[].name // empty' 2>/dev/null || true)
        elif [[ "${bucket_count}" -eq 0 && "${object_count}" -eq 0 ]]; then
            # Unknown provenance, but the Keystone project is confirmed gone
            # (rgw_find_orphaned_users only emits authoritative 404s; inconclusive
            # lookups raise _RGW_LISTING_ERRORS and abort the purge) AND the user
            # holds zero buckets/objects — so there is no data to lose. Keystone
            # implicit-tenant users self-heal: Ceph recreates them on the next
            # authenticated access. An empty dangling user is therefore always
            # safe to reap, which clears pre-provenance-ledger orphans the
            # rally-owned gate cannot vouch for. Unknown-owner users WITH data are
            # still skipped below — those need a human.
            log "RGW user ${uid}: orphaned (unknown owner, EMPTY — project gone, 0 buckets/objects; eligible)"
            TOTAL_FOUND=$((TOTAL_FOUND + 1))
            _RGW_EMPTY_ORPHANS=$((_RGW_EMPTY_ORPHANS + 1))
            _SNAP_RGW_USERS+=("${uid}")
        else
            _RGW_SKIPPED_UNKNOWN=$((_RGW_SKIPPED_UNKNOWN + 1))
            log "RGW user ${uid}: SKIPPED (unknown owner WITH data, ${bucket_count} buckets, ${object_count} objects)"
        fi
    done < "${orphans_file}"

    rm -f "${orphans_file}"

    if [[ "${listed_any}" -eq 0 ]]; then
        log "RGW users: none"
    elif [[ "${#_SNAP_RGW_USERS[@]}" -eq 0 && "${_RGW_SKIPPED_UNKNOWN}" -gt 0 ]]; then
        log "RGW users: no Rally-owned orphans eligible for purge"
    fi
}

purge_rgw_delete() {
    if [[ "${_RGW_LISTING_ERRORS}" -gt 0 ]]; then
        # shellcheck disable=SC2034  # read by purge_orphans.sh main
        RGW_PURGE_SKIPPED=true
        log "RGW purge skipped: ${_RGW_LISTING_ERRORS} RGW listing error(s) occurred during Phase 1."
        log "Investigate RGW or Keystone connectivity before retrying."
        return
    fi

    [[ "${#_SNAP_RGW_USERS[@]}" -eq 0 ]] && return

    # One DELETE per user. Rally-owned users go with purge-data, so RGW drops
    # their buckets via the user's own (tenant-correct) bucket listing. The
    # buckets are re-inventoried right before each delete (_rgw_inventory) so
    # counts and the audit log describe what purge-data actually removes, and
    # buckets created since Phase 1 still count against MAX_DELETE_THRESHOLD.
    # Empty unknown-owner users go WITHOUT purge-data and are skipped if the
    # inventory shows any bucket (RGW would refuse anyway).
    local uid entry purge_data snap_count extra rc
    local -a current
    for uid in "${_SNAP_RGW_USERS[@]}"; do
        purge_data="${_RGW_PURGE_DATA["${uid}"]:-false}"

        if _rgw_inventory "${uid}"; then rc=0; else rc=$?; fi
        if [[ "${rc}" -eq 2 ]]; then
            log "  RGW user ${uid}: already absent (HTTP 404); nothing deleted"
            continue
        elif [[ "${rc}" -ne 0 ]]; then
            log "  RGW user ${uid}: deletion SKIPPED (${_RGW_INVENTORY_ERROR})"
            TOTAL_FAILED=$((TOTAL_FAILED + 1))
            continue
        fi
        current=("${_RGW_INVENTORY[@]+"${_RGW_INVENTORY[@]}"}")

        if [[ "${purge_data}" != "true" && "${#current[@]}" -gt 0 ]]; then
            log "  RGW user ${uid}: deletion SKIPPED (unknown owner now owns ${#current[@]} bucket(s))"
            TOTAL_FAILED=$((TOTAL_FAILED + 1))
            continue
        fi

        snap_count=0
        for entry in "${_SNAP_RGW_BUCKETS[@]+"${_SNAP_RGW_BUCKETS[@]}"}"; do
            if [[ "${entry%%|*}" == "${uid}" ]]; then
                snap_count=$((snap_count + 1))
            fi
        done
        extra=$(( ${#current[@]} > snap_count ? ${#current[@]} - snap_count : 0 ))
        if [[ "${extra}" -gt 0 ]] && ! $FORCE \
                && (( TOTAL_FOUND + _RGW_PHASE2_EXTRA + extra > MAX_DELETE_THRESHOLD )); then
            log "  RGW user ${uid}: deletion SKIPPED (${extra} bucket(s) created since Phase 1 would exceed MAX_DELETE_THRESHOLD=${MAX_DELETE_THRESHOLD}; pass --force)"
            TOTAL_FAILED=$((TOTAL_FAILED + 1))
            continue
        fi
        _RGW_PHASE2_EXTRA=$((_RGW_PHASE2_EXTRA + extra))

        if rgw_delete_user "${uid}" "${purge_data}"; then rc=0; else rc=$?; fi
        case "${rc}" in
            0)
                log "  RGW user ${uid}: deleted (with ${#current[@]} bucket(s))"
                _rgw_record_purged "${uid}" "${current[@]+"${current[@]}"}"
                TOTAL_DELETED=$((TOTAL_DELETED + 1))
                ;;
            2)
                log "  RGW user ${uid}: already absent (HTTP 404); nothing deleted"
                ;;
            3)
                _rgw_settle_failed_delete "${uid}" true "${current[@]+"${current[@]}"}"
                ;;
            *)
                _rgw_settle_failed_delete "${uid}" false "${current[@]+"${current[@]}"}"
                ;;
        esac
    done
}

# Re-inventory a user's buckets into _RGW_INVENTORY. Authoritative source is
# the stats-free listing (rgw_list_user_bucket_names); a fresh stats listing
# must name exactly the same buckets, or the user is skipped -- with stats,
# Reef/Quincy silently omit buckets whose stats failed, so a disagreement
# means the snapshot/threshold/audit could be short of what purge-data would
# delete. Returns 0 ok, 2 user absent, 1 failure (_RGW_INVENTORY_ERROR says why).
_RGW_INVENTORY=()
_RGW_INVENTORY_ERROR=""
_rgw_inventory() {
    local uid="$1" names stats_json stats_names rc=0
    _RGW_INVENTORY=()
    names=$(rgw_list_user_bucket_names "${uid}") || rc=$?
    if [[ "${rc}" -eq 2 ]]; then return 2; fi
    if [[ "${rc}" -ne 0 ]]; then
        _RGW_INVENTORY_ERROR="bucket inventory failed"
        return 1
    fi
    if ! stats_json=$(rgw_list_user_buckets "${uid}") \
            || ! stats_names=$(jq -r '.[].name // empty' <<< "${stats_json}" 2>/dev/null); then
        _RGW_INVENTORY_ERROR="bucket stats listing failed"
        return 1
    fi
    if [[ "$(LC_ALL=C sort <<< "${names}")" != "$(LC_ALL=C sort <<< "${stats_names}")" ]]; then
        _RGW_INVENTORY_ERROR="bucket inventories disagree (stats listing omitted or added buckets)"
        return 1
    fi
    if [[ -n "${names}" ]]; then
        mapfile -t _RGW_INVENTORY <<< "${names}"
    fi
}

_rgw_record_purged() {
    local uid="$1" bucket; shift
    for bucket in "$@"; do
        _RGW_PURGED_BUCKETS+=("${uid}|${bucket}")
        TOTAL_DELETED=$((TOTAL_DELETED + 1))
    done
}

# A failed purge-data request may have removed some buckets, or even the
# user, before failing. Look again and record what is actually gone.
#
# Not a timeout: one look; the removed buckets count as purged, the rest as
# failed. After a client-side TIMEOUT RGW may still be deleting, and a stable
# bucket list proves nothing (objects inside may still be going), so the ONLY
# definitive outcome is the user being gone (authoritative 404): re-check with
# backoff through the whole budget (RGW_SETTLE_DELAYS, default
# 10+20+40+80+150 s = 5 min) and, unless the user disappears, record it as
# INDETERMINATE -- counted in TOTAL_INDETERMINATE and the audit log, neither
# failed nor deleted. A look that fails (cannot tell) is INDETERMINATE too.
RGW_SETTLE_DELAYS="${RGW_SETTLE_DELAYS:-10 20 40 80 150}"
_rgw_settle_failed_delete() {
    local uid="$1" timed_out="$2" rc bucket remaining="" state=""
    shift 2
    local -a before=("$@") purged=() left=() delays=()
    [[ "${timed_out}" == "true" ]] && read -r -a delays <<< "${RGW_SETTLE_DELAYS}"

    # state: gone | settled (non-timeout partial) | unknown (last look
    # failed) | pending (timeout, user still present).
    local i
    for (( i = 0; i <= ${#delays[@]}; i++ )); do
        if (( i > 0 )); then
            sleep "${delays[i - 1]}"
        fi
        if rgw_user_exists "${uid}"; then rc=0; else rc=$?; fi
        if [[ "${rc}" -eq 1 ]]; then
            state=gone
            break
        fi
        if [[ "${rc}" -ne 0 ]]; then
            state=unknown
            continue
        fi
        if [[ "${timed_out}" == "true" ]]; then
            state=pending
            continue
        fi
        if remaining=$(rgw_list_user_bucket_names "${uid}"); then
            state=settled
        else
            state=unknown
        fi
    done

    case "${state}" in
        gone)
            log "  RGW user ${uid}: delete request failed, but the user is gone; counted as deleted"
            _rgw_record_purged "${uid}" "${before[@]+"${before[@]}"}"
            TOTAL_DELETED=$((TOTAL_DELETED + 1))
            return
            ;;
        unknown|pending)
            local why="re-check failed"
            [[ "${state}" == "pending" ]] && why="user still present after the timeout re-check budget"
            log "  RGW user ${uid}: deletion outcome INDETERMINATE for its ${#before[@]} bucket(s) (${why})"
            _RGW_INDETERMINATE+=("${uid}")
            TOTAL_INDETERMINATE=$((TOTAL_INDETERMINATE + 1))
            return
            ;;
    esac

    local -A still=()
    while IFS= read -r bucket; do
        [[ -n "${bucket}" ]] && still["${bucket}"]=1
    done <<< "${remaining}"
    for bucket in "${before[@]+"${before[@]}"}"; do
        if [[ -n "${still["${bucket}"]+x}" ]]; then
            left+=("${bucket}")
        else
            purged+=("${bucket}")
        fi
    done
    log "  RGW user ${uid}: deletion FAILED (${#purged[@]} bucket(s) removed before the failure, ${#left[@]} remain)"
    _rgw_record_purged "${uid}" "${purged[@]+"${purged[@]}"}"
    TOTAL_FAILED=$((TOTAL_FAILED + 1 + ${#left[@]}))
}
