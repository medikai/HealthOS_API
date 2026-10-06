"""Patient portal authentication domain (phone OTP, sessions, record links).

Patient principals are deliberately separate from staff ``UserAccount``. Phone
OTP proves control of a phone number only; it never grants clinical-record
access. Record access comes from audited ``patient_record_link`` rows created
through clinic-authorized activation or staff-reviewed reconciliation.
"""
