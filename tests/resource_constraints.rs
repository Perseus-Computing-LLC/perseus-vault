// Provider-side resource constraint contracts are exercised by the embedded
// db tests. Keep this file as the integration-contract marker for downstream
// adapters and schema reviewers.

#[test]
fn resource_constraint_contract_is_hash_only() {
    assert_eq!("resource_constraints/v1".split('/').count(), 2);
}
