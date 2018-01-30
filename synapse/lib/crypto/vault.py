import os
import hashlib
import logging
import contextlib

import synapse.exc as s_exc
import synapse.common as s_common
import synapse.eventbus as s_eventbus

import synapse.lib.kv as s_kv
import synapse.lib.msgpack as s_msgpack

import synapse.lib.crypto.rsa as s_rsa

logger = logging.getLogger(__name__)

uservault = '~/.syn/vault.lmdb'

class Cert:

    def __init__(self, cert, rkey=None):

        self.cert = cert
        self.rkey = rkey

        self.tokn = s_msgpack.un(cert[0])
        self.toknhash = hashlib.sha256(cert[0]).hexdigest()

        byts = self.tokn.get('rsa:pub')
        self.rpub = s_rsa.PubKey.load(byts)

    def iden(self):
        '''
        Get the iden for the certificate.

        Returns:
            str: Iden of the certificate.
        '''
        return self.toknhash

    def getkey(self):
        '''
        Get the private RSA key for the certificate.

        Returns:
            s_rsa.PriKey: Private RSA Key. If not present, this returns None.
        '''
        return self.rkey

    def signers(self):
        '''
        Get the signing chain for the Cert.

        Returns:
            tuple: A tuple of tuples; the inner tuples contain iden, data bytes
            and signature bytes.
        '''
        return self.cert[1].get('signers')

    def public(self):
        '''
        Get the Public RSA key for the Cert

        Returns:
            s_rsa.PubKey: The Public RSA key.
        '''
        return self.rpub

    def toknbytes(self):
        '''
        Get the token bytes for the certificate.

        Returns:
            bytes: The msgpack encoded certificate token dictionary.
        '''
        return self.cert[0]

    def sign(self, cert, **info):
        '''
        Sign a certificate with the current Cert.

        Args:
            cert (Cert): Certificate to sign with the current Cert.
            **info: Additional data to include in the signed message.

        Returns:
            None
        '''

        if self.rkey is None:
            raise s_exc.NoCertKey(mesg='sign() requires a private key')

        info['time'] = s_common.now()

        data = s_msgpack.en(info)
        tosign = data + cert.toknbytes()

        sign = self.rkey.sign(tosign)

        signer = (self.iden(), data, sign)
        cert.addsigner(signer)

    def addsigner(self, sign):
        '''
        Append a new signature tuple to the current Cert's signers.

        Args:
            sign ((str, bytes, bytes)): Signature tuple to add the Cert.

        Returns:
            None
        '''
        self.cert[1]['signers'] += (sign,)

    def verify(self, byts, sign):
        '''
        Verify that the the Cert signed a set of bytes.

        Args:
            byts (bytes): Data to check.
            sign (bytes): Signature to verify.

        Returns:
            bool: True if the Cert signed the byts, False otherwise.
        '''
        return self.rpub.verify(byts, sign)

    def signed(self, cert):
        '''
        Check if this cert signed the given Cert and return the info.

        Args:
            cert (Cert): A Cert to confirm that we signed.

        Returns:
            dict: The signer info dict ( or None if not signed ).
        '''
        byts = cert.toknbytes()

        for iden, data, sign in cert.signers():

            if iden != self.iden():
                continue

            if self.verify(data + byts, sign):
                return s_msgpack.un(data)

    def save(self):
        '''
        Serialize the certificate to bytes for storage.

        Returns:
            bytes: A msgpack encoded form of the Cert.
        '''
        return s_msgpack.en(self.cert)

    @staticmethod
    def load(byts, rkey=None):
        '''
        Create a Cert object from the bytes.

        Args:
            byts (bytes): Bytes from a previously saved Cert
            rkey (s_rsa.PriKey): The RSA Private Key assocaited with the Cert.

        Returns:
            Cert: A Cert object for the bytes and RSA private key.
        '''
        return Cert(s_msgpack.un(byts), rkey=rkey)

class Vault(s_eventbus.EventBus):

    '''
    tokn:
        {
            'user': <str>,
            'rsa:pub': <bytes>,
        }

    cert:
        ( <toknbyts>, {
            "signers": (
                <sig>,
            ),
        })

    sig:
        # NOTE: <iden> must *only* be used for pub key lookup
        (<iden>, <bytes(signdata)>, <signbytes>),

    '''
    def __init__(self, path):

        s_eventbus.EventBus.__init__(self)

        self.kvstor = s_kv.KvStor(path)
        self.onfini(self.kvstor.fini)

        self.info = self.kvstor.getKvLook('info')  # Internal Housekeeping
        self.keys = self.kvstor.getKvLook('keys')  # iden -> private RSA keys
        self.certs = self.kvstor.getKvLook('certs')  # iden -> signed Cert
        self.roots = self.kvstor.getKvLook('roots')  # iden -> CAs capable of signing
        self.certkeys = self.kvstor.getKvLook('keys:bycert')  # cert -> private RSA keys
        self.usercerts = self.kvstor.getKvLook('certs:byuser')  # user -> certs

    def genRsaKey(self):
        '''
        Generate a new RSA key and store it in the vault.

        Returns:
            s_rsa.PriKey: The new RSA key.
        '''
        rkey = s_rsa.PriKey.generate()

        iden = rkey.iden()
        self.keys.set(iden, rkey.save())

        return rkey

    @staticmethod
    def genCertTokn(rpub, **info):
        '''
        Generate a public key certificate token.

        Args:
            rpub (s_rsa.PubKey):
            **info: Additional key/value data to be added to the certificate token.

        Returns:
            bytes: A msgpack encoded dictionary.
        '''
        info['rsa:pub'] = rpub.save()
        info['created'] = s_common.now()
        return s_msgpack.en(info)

    def genToknCert(self, tokn, rkey=None):
        '''
        Generate Cert object for a given token and RSA Private key.

        Args:
            tokn (bytes): Token which will be signed.
            rkey s_rsa.PriKey: RSA Private key used to sign the token.

        Returns:
            Cert: A Cert object
        '''
        cefo = (tokn, {'signers': ()})
        cert = Cert(cefo, rkey=rkey)

        cert.sign(cert)
        return cert

    def getUserCert(self, name):
        '''
        Retrieve a cert tufo for the given user.

        Args:
            name (str): Name of the user to retrieve the certificate for.

        Returns:
            Cert: User's certificate object, or None.
        '''
        iden = self.usercerts.get(name)
        if iden is None:
            return None

        return self.getCert(iden)

    def genUserCert(self, name):
        '''
        Generate a key/cert for the given user.

        Args:
            name (str): The user name.

        Returns:
            Cert: A newly generated user certificate.
        '''
        iden = self.usercerts.get(name)
        if iden is not None:
            return self.getCert(iden)

        rkey = self.genRsaKey()

        rpub = rkey.public()
        tokn = self.genCertTokn(rpub, user=name)
        cert = self.genToknCert(tokn, rkey=rkey)

        root = self.genRootCert()
        root.sign(cert)

        iden = cert.iden()

        self.certs.set(iden, cert.save())
        self.certkeys.set(iden, rkey.save())
        self.usercerts.set(name, iden)

        return cert

    def genUserAuth(self, user):
        '''
        Generate a *sensitive* user auth data structure.

        Args:
            user (str): The user name to generate the auth data for.

        Notes:
            The data returned by this API contains the user certificate and
            private key material. It is a sensitive data structure and care
            should be taken as to what happens with the output of this API.

        Returns:
            ((str, dict)): A tufo containing the user name and a dictionary
             of certificate and key material.
        '''
        cert = self.genUserCert(user)
        rkey = self.getCertKey(cert.iden())

        return (user, {
            'cert': cert.save(),
            'rsa:key': rkey.save(),
        })

    def addUserAuth(self, auth):
        '''
        Store a private user auth tufo.

        Args:
            auth ((str, dict)): A user auth tufo obtained via the genUserAuth API.

        Notes:
            This is a *sensitive* API. Auth tufos should only be loaded from
            trusted sources.
            This API is primarily designed for provisioning automation.

        Returns:
            Cert: Cert object derived from the auth tufo.
        '''
        user, info = auth

        certbyts = info.get('cert')
        rkeybyts = info.get('rsa:key')

        rkey = s_rsa.PriKey.load(rkeybyts)
        cert = Cert.load(certbyts, rkey=rkey)

        iden = cert.iden()

        self.certs.set(iden, cert.save())
        self.certkeys.set(iden, rkey.save())
        self.usercerts.set(user, iden)

        return cert

    def getCert(self, iden):
        '''
        Get a certificate by iden.

        Args:
            iden (str): The cert iden.

        Returns:
            Cert: The Cert or None.
        '''
        byts = self.certs.get(iden)
        if byts is None:
            return None

        rkey = self.getCertKey(iden)
        return Cert.load(byts, rkey=rkey)

    def getCertKey(self, iden):
        '''
        Get the RSA Private Key for a given iden.

        Args:
            iden (str): Iden to retrieve

        Returns:
            s_rsa.PriKey: The RSA Private Key object.
        '''
        byts = self.certkeys.get(iden)
        if byts is not None:
            return s_rsa.PriKey.load(byts)

    def genRootCert(self):
        '''
        Get or generate the primary root cert for this vault.

        Returns:
            Cert: A cert helper object
        '''
        iden = self.info.get('root')
        if iden is not None:
            return self.getCert(iden)

        rkey = self.genRsaKey()
        tokn = self.genCertTokn(rkey.public())
        cert = self.genToknCert(tokn, rkey=rkey)

        iden = cert.iden()

        self.info.set('root', iden)

        self.certkeys.set(iden, rkey.save())

        self.addRootCert(cert)
        return cert

    def addRootCert(self, cert):
        '''
        Add a certificate to the Vault as a root certificate.

        Args:
            cert (Cert): Certificate to add to the Vault.

        Returns:
            None
        '''
        iden = cert.iden()
        self.roots.set(iden, True)
        self.certs.set(iden, cert.save())

    def delRootCert(self, cert):
        '''
        Delete a root certificate from the Vault.

        Args:
            cert (Cert): Certificate for the root CA to remove.

        Returns:
            None
        '''
        iden = cert.iden()
        self.roots.set(iden, False)

    def getRootCerts(self):
        '''
        Get a list of the root certificates from the Vault.

        Returns:
            list: A list of root certificates as Cert objects.
        '''
        retn = []
        for iden, isok in self.roots.items():

            if not isok:
                continue

            cert = self.getCert(iden)
            if cert is None:  # pragma: no cover
                # This is a unusual case since cert being added to self.roots
                # is going to add the cert to self.certs
                continue

            retn.append(cert)

        return retn

    def isValidCert(self, cert):
        '''
        Check if a Vault can validate a Cert against its root certificates.

        Args:
            cert (Cert): Cert to check against.

        Returns:
            bool: True if the certificate is valid, False otherwise.
        '''
        return any([c.signed(cert) for c in self.getRootCerts()])

@contextlib.contextmanager
def shared(path):
    '''
    A context manager for locking a potentially shared vault.

    Args:
        path (str): Path to the vault.

    Example:

        with s_vault.shared('~/.syn/vault') as vault:
            dostuff()

    Yields:
        Vault: A Vault object.
    '''
    full = s_common.genpath(path)
    lock = os.path.join(full, 'synapse.lock')

    with s_common.lockfile(lock):
        with Vault(full) as vault:
            yield vault
