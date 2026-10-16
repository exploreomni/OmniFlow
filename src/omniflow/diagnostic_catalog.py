"""Fixed explanations only: never interpolate customer text into public messages."""

LINT_MESSAGES = {
    "require_field_descriptions": "Field description is missing. Add descriptions to the affected fields in model YAML.",
    "require_measure_descriptions": "Measure description is missing. Document the affected measures in model YAML.",
    "require_primary_keys": "A view has no declared primary key. Verify its row grain and declare the correct primary key.",
    "require_topic_labels": "Topic label is missing. Add a readable label to the affected topics.",
    "require_owner_metadata": "Topic owner metadata is missing. Add documented owners to the affected topics.",
    "forbid_many_to_many_without_comment": (
        "A many-to-many relationship needs governance review. Verify join grain and fanout; "
        "record the review without inventing an unsupported YAML comment property."
    ),
    "block_deleted_fields": "A field was deleted. Review downstream references before accepting the removal.",
    "warn_field_type_change": "A field type changed. Review dependent filters, calculations, and content for compatibility.",
    "warn_measure_aggregation_change": "A measure aggregation changed. Verify the intended calculation and dependent metrics.",
    "warn_relationship_cardinality_change": "Join cardinality changed. Verify join grain, fanout, and dependent metrics.",
    "forbid_personal_folder_validation_scope": (
        "Personal-folder content is included in CI validation. Review omni.include_personal_folders "
        "against the intended governed scope."
    ),
}

CHANGE_MESSAGES = {
    **{f"{kind}_{change}": f"A {kind} was {change}. Review the semantic diff and downstream dependencies."
       for kind in ("field", "view", "topic", "relationship") for change in ("added", "deleted", "modified")},
    "field_renamed": "A field may have been renamed. Review old references and update dependent content as needed.",
    "file_renamed": "A semantic file was renamed. Verify repository paths and model synchronization.",
    "field_type_changed": "A field type changed. Check dependent filters, calculations, and content for compatibility.",
    "field_kind_changed": "A field changed between semantic kinds. Review dimension/measure usage in dependent content.",
    "measure_aggregation_changed": "A measure aggregation changed. Verify intended calculations and dependent metrics.",
    "relationship_cardinality_changed": "Join cardinality changed. Verify join grain, fanout, and dependent metrics.",
    "governance_changed": "Model governance settings changed. Review access rules and ownership before accepting the change.",
}

CHECK_GUIDANCE = {
    "context": "Review trusted model metadata and branch selection.",
    "setup": "Review required policy files and credential configuration; do not share secret values.",
    "execution": "Review check execution status and share the run reference with the OmniFlow maintainer.",
    "content": "Open Content Validator in Omni for the same model and branch to inspect the affected content.",
    "model": "Validate the same model branch in Omni and inspect the reported model definition errors.",
    "candidate": "Verify that the selected Omni model and branch match the intended Git revisions.",
    "semantic_diff": "Review the semantic diff for the selected base and candidate.",
    "semantic_lint": "Review the semantic lint policy and model YAML; this finding has no recognized lint rule code.",
    "contracts": "Review downstream dependency coverage and the configured contract policy.",
    "downstream": "Review dependency lookup coverage and content read permissions.",
    "dbt_exposures": "Review dbt exposure coverage and required connection permissions.",
    "dbt_impact": "Review the changed dbt resources and their mapped Omni dependencies.",
    "dbt_sync": "Review refresh and post-sync validation status before considering deployment complete.",
    "ai_eval": "Inspect the AI evaluation and cleanup status in the authorized Omni environment.",
    "ai_repair": "Inspect repair and rollback status before accepting any proposed change.",
    "breaking_change_hold": "Review deployment synchronization and current-head revalidation before releasing the hold.",
    "reporting": "Check artifact output permissions and use only fresh artifacts from this run.",
}
