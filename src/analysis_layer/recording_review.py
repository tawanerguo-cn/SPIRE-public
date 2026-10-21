"""Deterministic presentation annotations over immutable recording evidence.

Priority describes usefulness in an operator summary, never scientific validity
or permission to discard data. This module does no I/O and controls no device.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace

from .spectrum_images import spectrum_channel_quality


REVIEW_VERSION = "recording-review-v3"
PROCESS_FIELDS = {
    "auto_approach_state_change": ("auto_approach", "auto_approach_running"),
    "scan_state_change": ("scan", "scan_running"),
    "bias_spectroscopy_state_change": ("bias_spectroscopy", "bias_spectroscopy_running"),
}


def _payload(action):
    arguments = action.get("arguments") or {}
    value = arguments.get("value")
    return value if isinstance(value, dict) and not arguments.get("truncated") else {}


def local_time(utc_ns):
    return datetime.fromtimestamp(utc_ns / 1e9, timezone.utc).astimezone().isoformat(timespec="milliseconds") if utc_ns else None


def tip_shaper_observation_context(actions, episodes):
    """Closed observed feedback intervals; process states are not action labels."""
    intervals, start = [], None
    for action in sorted(actions, key=lambda item: item["monotonic_ns"]):
        if action["phase"] != "observed" or action.get("error_type"):
            continue
        if action["action_name"] not in {"z_controller_state_change", "withdraw", "instrument_state_snapshot"}:
            continue
        on = _payload(action).get("after", {}).get("z_controller_on")
        if on is False and start is None:
            start = action
        elif on is True and start is not None:
            intervals.append({"start_monotonic_ns": start["monotonic_ns"],
                              "end_monotonic_ns": action["monotonic_ns"],
                              "start_action_event_id": start["action_event_id"],
                              "end_action_event_id": action["action_event_id"]})
            start = None
    return {"feedback_intervals": intervals,
            "excluded_process_intervals": [{key: episode[key] for key in
                ("kind", "start_monotonic_ns", "end_monotonic_ns")} for episode in episodes
                if episode["kind"] in {"auto_approach", "bias_spectroscopy"}]}


def saved_recording_file_quality(saved, *, run_started_utc_ns):
    """Reuse channel quality checks; completeness is numerical, not experiment success."""
    if hasattr(saved, "columns") and hasattr(saved, "data"):
        channels = spectrum_channel_quality(SimpleNamespace(channel_names=saved.columns, data=saved.data.T))
        return {"channels": channels, "all_channels_finite": bool(channels) and all(q["point_count"] > 0 and q["finite_count"] == q["point_count"] for q in channels.values()),
                "notes": ["Lock-in channels do not establish calibrated dI/dV or correct modulation."]}
    arrays = [(f"{channel.name}/{direction}", data.ravel()) for channel in saved.channels
              for direction in ("forward", "backward") if (data := getattr(channel, direction)) is not None]
    channels = spectrum_channel_quality(SimpleNamespace(channel_names=[name for name,_ in arrays], data=[data for _,data in arrays]))
    try:
        acquisition_start = datetime.strptime(saved.recorded_date + " " + saved.recorded_time, "%d.%m.%Y %H:%M:%S").astimezone()
        predates_run = acquisition_start.timestamp() < run_started_utc_ns / 1e9 - 2
    except ValueError:
        predates_run = None
    return {"channels": channels, "all_channels_finite": bool(channels) and all(q["point_count"] > 0 and q["finite_count"] == q["point_count"] for q in channels.values()),
            "acquisition_header_time": saved.recorded_date + " " + saved.recorded_time,
            "acquisition_predates_run": predates_run,
            "notes": ["A saved cached scan can predate this run; file names do not establish specimen identity."]}


def summarize_recording(snapshot):
    actions = snapshot["actions"]
    episodes, active = [], {}
    for action in actions:
        if action["phase"] != "observed":
            continue
        mapping = PROCESS_FIELDS.get(action["action_name"])
        if not mapping:
            continue
        kind, field = mapping
        value = _payload(action).get("after", {}).get(field)
        if value is True and kind not in active:
            episode = {"id": f"{kind}-{action['action_event_id']}", "kind": kind,
                       "start_utc_ns": action["utc_ns"], "start_monotonic_ns": action["monotonic_ns"],
                       "end_utc_ns": None, "end_monotonic_ns": None,
                       "start_action_event_id": action["action_event_id"], "end_action_event_id": None,
                       "completion": "running_observed_no_end", "physical_success_confirmed": False}
            active[kind] = episode
            episodes.append(episode)
        elif value is False and kind in active:
            episode = active.pop(kind)
            episode.update(end_utc_ns=action["utc_ns"], end_monotonic_ns=action["monotonic_ns"],
                           end_action_event_id=action["action_event_id"], completion="running_state_ended")

    annotations, grouped = [], defaultdict(list)
    for action in actions:
        payload = _payload(action)
        after = payload.get("after", {})
        kind = action["action_name"]
        priority, reason = "normal", "distinct_observation_or_action"
        if action["phase"] == "failed" or action.get("error_type") or action.get("intent") == "protective" or kind == "withdraw":
            priority, reason = "attention", "failure_or_protective_transition"
        annotation = {"action_event_id": action["action_event_id"], "action_id": action["action_id"],
                      "action_name": kind, "utc_ns": action["utc_ns"], "priority": priority,
                      "reason": reason, "group_id": None}
        # Never demote explicit operator/SPIRE commands, failures, unknown payloads,
        # SafeTip, or transitions outside a positively observed approach interval.
        if (action["phase"] == "observed" and action["actor"] == "unknown" and payload
                and not action.get("error_type") and action.get("intent") != "protective"
                and kind in {"withdraw", "z_controller_state_change", "z_position_change"}
                and after.get("z_controller_status") in {None, "withdrawing", "on", "off", "hold"}):
            for episode in episodes:
                if (episode["kind"] == "auto_approach" and episode["start_monotonic_ns"] < action["monotonic_ns"]
                        and (episode["end_monotonic_ns"] is None or action["monotonic_ns"] < episode["end_monotonic_ns"])):
                    grouped[(episode["id"], kind)].append(annotation)
                    break
        annotations.append(annotation)
    groups = []
    for (episode_id, kind), members in grouped.items():
        if len(members) < 2:
            continue
        group_id = f"{episode_id}/{kind}"
        for member in members:
            member.update(priority="background", reason="repeated_state_within_observed_auto_approach", group_id=group_id)
        groups.append({"group_id": group_id, "episode_id": episode_id, "action_name": kind,
                       "priority": "background", "reason": "repeated_state_within_observed_auto_approach",
                       "count": len(members), "first_utc_ns": members[0]["utc_ns"], "last_utc_ns": members[-1]["utc_ns"],
                       "action_event_ids": [member["action_event_id"] for member in members],
                       "interpretation": "Internal state observations, not this many manual operations; retain raw evidence."})

    for episode in episodes:
        episode["internal_state_counts"] = {g["action_name"]: g["count"] for g in groups if g["episode_id"] == episode["id"]}
    timeline = [{"kind": e["kind"], "utc_ns": e["start_utc_ns"], "end_utc_ns": e["end_utc_ns"],
                 "priority": "normal" if e["end_utc_ns"] else "attention", "episode_id": e["id"],
                 "evidence": {"start_action_event_id": e["start_action_event_id"], "end_action_event_id": e["end_action_event_id"]},
                 "internal_state_counts": e["internal_state_counts"], "completion": e["completion"]} for e in episodes]
    scan_ends = [e["end_monotonic_ns"] for e in episodes if e["kind"] == "scan" and e["end_monotonic_ns"] is not None]
    for action, annotation in zip(actions, annotations):
        if annotation["priority"] == "background":
            continue
        # Process boundaries are already represented by episodes. File events
        # are represented by the artifact inventory, not counted as new runs.
        if action["action_name"] in PROCESS_FIELDS or action["action_name"] in {"scan_acquire", "bias_spectroscopy_acquire", "instrument_state_snapshot", "tip_shaper_parameters"}:
            if action["phase"] == "observed":
                continue
        entry = {"kind": action["action_name"], "utc_ns": action["utc_ns"], "priority": annotation["priority"],
                 "action_event_id": action["action_event_id"], "actor": action["actor"], "phase": action["phase"],
                 "change": _payload(action)}
        if action.get("error_type"):
            entry["error"] = {"type": action["error_type"], "message": action.get("error_message")}
        if action["action_name"] == "follow_me_move":
            entry["interpretation"] = "Position read-back; manual intent is unconfirmed."
            if any(0 <= action["monotonic_ns"] - end < 1_000_000_000 for end in scan_ends):
                entry["interpretation"] = "Within 1 s after scan stop; scanner settling versus manual movement is ambiguous."
        timeline.append(entry)
    timeline.sort(key=lambda entry: entry["utc_ns"])
    stats = snapshot["sample_stats"].copy()
    wall_span = ((stats["last_monotonic_ns"] - stats["first_monotonic_ns"]) / 1e9
                 if stats["count"] > 1 else None)
    duration = stats["sampled_duration_ns"] / 1e9
    stats.update(duration_s=duration, wall_span_s=wall_span,
                 average_hz=stats["interval_count"] / duration if duration else None,
                 gap_events=snapshot["event_counts"].get("telemetry_gap", 0))
    attention = [{"kind": event["event_type"], "event_id": event["event_id"], "utc_ns": event["utc_ns"], "details": event["payload"]}
                 for event in snapshot["events"] if any(word in event["event_type"] for word in ("failed", "failure", "invalid", "quota", "high_watermark"))]
    if stats["gap_events"]:
        attention.append({"kind": "telemetry_cadence", "gap_events": stats["gap_events"],
                          "estimated_missed_deadlines": stats["estimated_missed_deadlines"],
                          "maximum_interval_ns": stats["maximum_interval_ns"],
                          "interpretation": "Gap warnings include timing jitter; warning count is not lost-sample count."})
    if any(snapshot["truncated"].values()):
        attention.append({"kind": "summary_input_truncated", "details": snapshot["truncated"]})
    run = snapshot["run"]
    return {"schema_version": 1, "analysis_version": REVIEW_VERSION, "authorizes_live_action": False,
            "run_id": run["run_id"], "status": run["status"], "finalized": run["stopped_utc_ns"] is not None,
            "process_running": "not_checked_database_status_is_not_process_liveness",
            "started_utc_ns": run["started_utc_ns"], "stopped_utc_ns": run["stopped_utc_ns"],
            "source": {"database_path": snapshot["database_path"], "watermarks": snapshot["watermarks"]},
            "counts": snapshot["counts"], "telemetry": stats, "episodes": episodes,
            "observed_process_counts": dict(Counter(e["kind"] for e in episodes)),
            "background_groups": groups, "annotation_counts": dict(Counter(a["priority"] for a in annotations)),
            "attention": attention, "timeline": timeline, "action_annotations": annotations,
            "segments": [event for event in snapshot["events"] if event["event_type"].startswith("high_resolution_segment_")],
            "event_aggregates": {"telemetry_gap": {"count": stats["gap_events"], "priority": "attention", "presentation": "aggregate_with_timing_metrics"},
                                 "tip_proximity_state_changed": {"count": snapshot["event_counts"].get("tip_proximity_state_changed", 0),
                                                                 "priority": "normal", "presentation": "aggregate_with_segment_boundaries"}},
            "limits": ["Background means low incremental information in this summary, not dispensable raw data.",
                       "Observed states do not identify who issued a command or prove physical success.",
                       "File presence does not prove fresh or complete acquisition.",
                       "A closed high-resolution segment does not mean the recorder process exited."]}


def render_recording_summary(review):
    labels = {"auto_approach": "自动进针", "scan": "扫描", "bias_spectroscopy": "偏压谱",
              "withdraw": "退回状态", "z_controller_state_change": "Z 反馈状态变化",
              "z_position_change": "Z 位置变化", "follow_me_move": "XY 位置变化",
              "bias_change": "偏压变化", "bias_set": "偏压设置", "tip_shaper_inferred": "扎针／Tip Shaper（自动识别）"}

    def name(value):
        return labels.get(value, value)

    def clock(value):
        return local_time(value) or "尚无结束记录"

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    def counts(values):
        return "、".join(f"{name(key)} {count:,} 次" for key, count in values.items()) or "无"

    def number(value, field):
        if isinstance(value, bool):
            return "开" if value else "关"
        if isinstance(value, (int, float)):
            if field.endswith("_m"):
                return f"{value * 1e9:.3f} nm"
            if field.endswith("_v"):
                return f"{value:.6g} V"
            return f"{value:.6g}"
        return str(value) if value is not None else "未记录"

    def file_quality(item):
        quality = item.get("quality", {})
        checks = {"verified": "SHA-256 与记录一致", "outside_read_roots": "未检查：不在读取范围",
                  "missing": "文件缺失", "file_check_budget_exceeded": "未检查：超过读取预算",
                  "source_changed": "文件内容已改变", "unreadable": "读取失败",
                  "not_scan_or_spectrum": "非扫描/谱文件"}
        detail = [checks.get(item.get("check"), item.get("check", "未检查"))]
        if quality.get("all_channels_finite") is False:
            detail.append("含未采集/非有限数据点")
        elif quality.get("all_channels_finite") is True:
            detail.append("所有已保存通道的数值完整")
        if quality.get("acquisition_predates_run"):
            detail.append(f"文件头采集时间早于本次记录：{quality['acquisition_header_time']}")
        return "；".join(detail)

    stats = review["telemetry"]
    frequency = f"{stats['average_hz']:.2f} Hz" if stats['average_hz'] is not None else "样本不足"
    lines = [f"# 实验记录摘要：{review['run_id']}", "",
             f"记录状态：`{review['status']}`；数据库已结束：{'是' if review['finalized'] else '否'}。此报告不检查进程存活。",
             f"开始：{clock(review['started_utc_ns'])}；结束：{clock(review['stopped_utc_ns'])}。", "",
             f"I/X/Y/Z：{stats['count']:,} 点；段内平均频率：{frequency}（排除段间空闲）。",
             f"观察到的过程：{counts(review['observed_process_counts'])}。",
             f"动作事件共 {review['counts']['action_events']:,} 条，其中 {review['annotation_counts'].get('background', 0):,} 条标记为过程内重复状态并折叠。", ""]
    recognition = review.get("tip_shaper_recognition")
    if recognition is not None:
        lines += [f"自动识别扎针／Tip Shaper 波形：{len(recognition['detections'])} 次。"
                  "操作名由 I/Z 轨迹推断；未匹配不代表没有操作，识别不评价修针效果。", ""]
    for event in review["segments"]:
        boundary = "开始" if event["event_type"].endswith("started") else "结束"
        lines.append(f"- 高频段{boundary}：{clock(event['utc_ns'])}；原因：`{event['payload'].get('reason')}`；事件 #{event['event_id']}。")
    lines += ["", "## 需要注意", ""]
    for item in review["attention"]:
        if item["kind"] == "telemetry_cadence":
            maximum = item['maximum_interval_ns']
            interval = f"{maximum / 1e6:.2f} ms" if maximum is not None else "未知"
            detail = (f"采样时序：{item['gap_events']:,} 条间隔告警，估计错过 {item['estimated_missed_deadlines']:,} 个采样期限；"
                      f"最大间隔 {interval}。告警次数不等于丢失样本数。")
        elif item["kind"] == "artifact_quality":
            filename = item["source_path"].replace("\\", "/").rsplit("/", 1)[-1]
            detail = f"`{filename}`：{file_quality(item)}。"
        else:
            details = item.get("details", {})
            detail = f"{item['kind']}：" + "；".join(f"{key}={value}" for key, value in details.items())
            if "event_id" in item:
                detail += f"（事件 #{item['event_id']}）"
        lines.append(f"- {detail}")
    if not review["attention"]:
        lines.append("本次检查未发现记录或文件质量告警；动作级重点状态见下表。")
    lines += ["", "## 操作时间线", "", "| 时间 | 过程/变化 | 内容 | 动作证据编号 |", "|---|---|---|---|"]
    for entry in review["timeline"]:
        span = clock(entry['utc_ns'])
        if "inference_id" in entry:
            detected = entry["detection"]
            span += f"（约 {(detected['end_monotonic_ns'] - detected['start_monotonic_ns']) / 1e9:.2f} s）"
            source = {"recorded_configuration": "匹配事件前参数快照（不证明执行时配置）",
                      "waveform_inferred": "位移由波形反推；时长使用参考模型，其他配置未知",
                      "reviewed_waveform_example": "参数未由接口证实，按波形模板匹配"}[detected["parameter_source"]]
            detail = (f"Z 接近约 {abs(detected['observed_z_dip_m']) * 1e9:.3f} nm；"
                      f"电流峰值 {detected['peak_current_a'] * 1e9:.3f} nA；{source}；"
                      "操作名由波形自动推断，修针效果未判断")
            if "observed_z_return_m" in detected:
                detail += f"；程序回撤约 {detected['observed_z_return_m'] * 1e9:.3f} nm"
            if detected.get("feedback_interval"):
                feedback = detected["feedback_interval"]
                detail += f"；关/开反馈证据 #{feedback['start_action_event_id']}/#{feedback['end_action_event_id']}"
            post = detected.get("post_return")
            if post:
                if post["extra_retraction_m"] is not None:
                    detail += f"；程序回撤后额外 Z 回撤 {post['extra_retraction_m'] * 1e9:.3f} nm（不等于 SafeTip 触发）"
                if not post["complete"]:
                    detail += "；末端响应观察窗口不完整"
            if detected["possible_current_clipping"]:
                detail += "；电流疑似达到量程平台"
            evidence = f"signal seq {detected['signal_seq_start']}–{detected['signal_seq_end']}"
        elif "episode_id" in entry:
            span += " 至 " + clock(entry['end_utc_ns'])
            detail = "观察到运行状态区间，不代表物理过程成功"
            if entry["internal_state_counts"]:
                detail += "；过程内状态：" + counts(entry["internal_state_counts"])
            evidence = f"{entry['evidence']['start_action_event_id']} / {entry['evidence']['end_action_event_id']}"
        else:
            change = entry.get("change", {})
            before, after = change.get("before", {}), change.get("after", {})
            # Observer changed_fields can use device-level names while its
            # before/after evidence uses shorter keys (e.g. Follow Me x_m).
            detail = "；".join(f"{field}: {number(before.get(field), field)} → {number(after.get(field), field)}"
                               for field in dict.fromkeys((*before, *after)))
            if entry.get("phase") != "observed":
                detail += f"；执行阶段 {entry.get('phase')}，操作者 {entry.get('actor')}"
            if entry.get("error"):
                detail += f"；{entry['error']['type']}: {entry['error']['message']}"
            evidence = str(entry["action_event_id"])
        if entry.get("interpretation"):
            detail += ("；距扫描停止不足 1 秒，扫描器回位或人工移动尚无法区分"
                       if entry["interpretation"].startswith("Within 1 s") else "；位置读回不能确认人工移动意图")
        label = name(entry['kind']) + ("（需关注）" if entry['priority'] == 'attention' else "")
        lines.append(f"| {span} | {label} | {cell(detail)} | {evidence} |")
    lines += ["", "## 折叠的过程内重复状态", ""]
    for group in review["background_groups"]:
        lines.append(f"- `{group['episode_id']}` 内：{name(group['action_name'])} {group['count']:,} 条，{clock(group['first_utc_ns'])} 至 {clock(group['last_utc_ns'])}。")
    lines += ["", "这些标签表示对当前摘要的信息增量较低，原始数据完整保留。逐条标签和原始编号见 `annotations.json`。",
              "", "## 保存文件", "", "| 文件 | 检查结果 | 各通道有限点数 / 总点数 |", "|---|---|---|"]
    for artifact in review.get("artifacts", []):
        filename = artifact["source_path"].replace("\\", "/").rsplit("/", 1)[-1]
        channels = artifact.get("quality", {}).get("channels", {})
        finite = "；".join(f"{name}: {q['finite_count']:,}/{q['point_count']:,}" for name, q in channels.items()) or "未检查"
        lines.append(f"| {cell(filename)} | {cell(file_quality(artifact))} | {cell(finite)} |")
    lines += ["", "## 证据与解释边界", "", f"原始数据库：`{review['source']['database_path']}`。",
              "完整路径、指标、来源编号与读取水位见 `summary.json`；本摘要不授权仪器操作。", "",
              "- 状态读回不能确定命令由谁发出，也不能证明物理过程成功。",
              "- 保存文件不等于全新或完整采集；文件名不能确定样品身份。",
              "- 数值完整不等于科学有效；Lock-in 通道存在不证明调制设置或 dI/dV 标定正确。",
              "- 高频段结束不等于记录进程退出。"]
    return "\n".join(lines) + "\n"
