#!/usr/bin/env python3
# create a BIP70 payment request signed with a certificate

import sys

import tlslite # pylint: disable=import-error

from electrumsv.transaction import Transaction
# from electrumsv import paymentrequest
from electrumsv import paymentrequest_pb2 as pb2
from electrumsv.address import Address

chain_file = 'mychain.pem'
cert_file = 'mycert.pem'
amount = 1000000
address = Address.from_string("18U5kpCAU4s8weFF8Ps5n8HAfpdUjDVF64")
memo = "blah"
out_file = "payreq"


with open(chain_file, 'r') as f:
    chain = tlslite.X509CertChain()
    chain.parsePemList(f.read())

# pylint: disable=no-member
certificates = pb2.X509Certificates()
certificates.certificate.extend(map(lambda x: str(x.bytes), chain.x509List))

with open(cert_file, 'r') as f:
    rsakey = tlslite.utils.python_rsakey.Python_RSAKey.parsePEM(f.read())

script = bytes.fromhex(Transaction.pay_script(address))

"""
# TODO rt12 -- fix this or delete this script. non-existent in electrum too.
pr_string = paymentrequest.make_payment_request(amount, script, memo, rsakey)

with open(out_file,'wb') as f:
    f.write(pr_string)

print("Payment request was written to file '%s'"%out_file)
"""

print("This does not work, was broken at some point.", file=sys.stderr)
