"""Test runner: drives coat_audit against audit_test.csv, leaving audit.csv untouched."""
import coat_audit

coat_audit.CSV_PATH = "audit_test.csv"
coat_audit.LOG_FILE = "coat_test_log.txt"
coat_audit.main()
