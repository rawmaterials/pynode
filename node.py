#!/usr/bin/python
#
# node.py - Bitcoin P2P network half-a-node
#
# Distributed under the MIT/X11 software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
#

import gevent
import gevent.pywsgi
from gevent import Greenlet

import signal
import struct
import socket
import binascii
import time
import sys
import re
import random
#import cStringIO
from io import StringIO
import io
import copy
import re
import hashlib
import rpc

import ChainDb
import MemPool
import Log
from bitcoin.core import *
from bitcoin.serialize import *
from bitcoin.messages import *

MY_SUBVERSION = "/pynode:0.0.1/"

settings = {}
debugnet = False


def verbose_sendmsg(message):
	if debugnet:
		return True
	if message.command != 'getdata':
		return True
	return False


def verbose_recvmsg(message):
	skipmsg = {
		'tx',
		'block',
		'inv',
		'addr',
	}
	if debugnet:
		return True
	if message.command in skipmsg:
		return False
	return True


class NodeConn(Greenlet):
	def __init__(self, dstaddr, dstport, log, peermgr,
			 mempool, chaindb, netmagic):
		Greenlet.__init__(self)
		self.log = log
		self.peermgr = peermgr
		self.mempool = mempool
		self.chaindb = chaindb
		self.netmagic = netmagic
		self.dstaddr = dstaddr
		self.dstport = dstport
		self.sock = gevent.socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.recvbuf = ""
		self.ver_send = MIN_PROTO_VERSION
		self.ver_recv = MIN_PROTO_VERSION
		self.last_sent = 0
		self.getblocks_ok = True
		self.last_block_rx = time.time()
		self.last_getblocks = 0
		self.remote_height = -1

		self.hash_continue = None

		self.log.write("connecting")
		try:
			self.sock.connect((dstaddr, dstport))
		except:
			self.handle_close()

		#stuff version msg into sendbuf
		vt = msg_version()
		vt.addrTo.ip = self.dstaddr
		vt.addrTo.port = self.dstport
		vt.addrFrom.ip = "0.0.0.0"
		vt.addrFrom.port = 0
		vt.nStartingHeight = self.chaindb.getheight()
		vt.strSubVer = MY_SUBVERSION
		self.send_message(vt)

	def _run(self):
		self.log.write(self.dstaddr + " connected")
		while True:
			try:
				t = self.sock.recv(8192)
				if len(t) <= 0: raise ValueError
			except (IOError, ValueError):
				self.handle_close()
				return
			self.recvbuf += t
			self.got_data()

	def handle_close(self):
		self.log.write(self.dstaddr + " close")
		self.recvbuf = ""
		try:
			self.sock.shutdown(socket.SHUT_RDWR)
			self.close()
		except:
			pass

	def got_data(self):
		while True:
			if len(self.recvbuf) < 4:
				return
			if self.recvbuf[:4] != self.netmagic.msg_start:
				raise ValueError("got garbage %s" % repr(self.recvbuf))
			# check checksum
			if len(self.recvbuf) < 4 + 12 + 4 + 4:
				return
			command = self.recvbuf[4:4+12].split("\x00", 1)[0]
			msglen = struct.unpack("<i", self.recvbuf[4+12:4+12+4])[0]
			checksum = self.recvbuf[4+12+4:4+12+4+4]
			if len(self.recvbuf) < 4 + 12 + 4 + 4 + msglen:
				return
			msg = self.recvbuf[4+12+4+4:4+12+4+4+msglen]
			th = hashlib.sha256(msg).digest()
			h = hashlib.sha256(th).digest()
			if checksum != h[:4]:
				raise ValueError("got bad checksum %s" % repr(self.recvbuf))
			self.recvbuf = self.recvbuf[4+12+4+4+msglen:]

			if command in messagemap:
				f = cStringIO.StringIO(msg)
				t = messagemap[command](self.ver_recv)
				t.deserialize(f)
				self.got_message(t)
			else:
				self.log.write("UNKNOWN COMMAND %s %s" % (command, repr(msg)))

	def send_message(self, message):
		if verbose_sendmsg(message):
			self.log.write("send %s" % repr(message))

		tmsg = message_to_str(self.netmagic, message)

		try:
			self.sock.sendall(tmsg)
			self.last_sent = time.time()
		except:
			self.handle_close()

	def send_getblocks(self, timecheck=True):
		if not self.getblocks_ok:
			return
		now = time.time()
		if timecheck and (now - self.last_getblocks) < 5:
			return
		self.last_getblocks = now

		our_height = self.chaindb.getheight()
		if our_height < 0:
			gd = msg_getdata(self.ver_send)
			inv = CInv()
			inv.type = 2
			inv.hash = self.netmagic.block0
			gd.inv.append(inv)
			self.send_message(gd)
		elif our_height < self.remote_height:
			gb = msg_getblocks(self.ver_send)
			if our_height >= 0:
				gb.locator.vHave.append(self.chaindb.gettophash())
			self.send_message(gb)

	def got_message(self, message):
		gevent.sleep()

		if self.last_sent + 30 * 60 < time.time():
			self.send_message(msg_ping(self.ver_send))

		if verbose_recvmsg(message):
			self.log.write("recv %s" % repr(message))

		if message.command == "version":
			self.ver_send = min(PROTO_VERSION, message.nVersion)
			if self.ver_send < MIN_PROTO_VERSION:
				self.log.write("Obsolete version %d, closing" % (self.ver_send,))
				self.handle_close()
				return

			if (self.ver_send >= NOBLKS_VERSION_START and
			    self.ver_send <= NOBLKS_VERSION_END):
				self.getblocks_ok = False

			self.remote_height = message.nStartingHeight
			self.send_message(msg_verack(self.ver_send))
			if self.ver_send >= CADDR_TIME_VERSION:
				self.send_message(msg_getaddr(self.ver_send))
			self.send_getblocks()

		elif message.command == "verack":
			self.ver_recv = self.ver_send

#			if self.ver_send >= MEMPOOL_GD_VERSION:
#				self.send_message(msg_mempool())

		elif message.command == "ping":
			if self.ver_send > BIP0031_VERSION:
				self.send_message(msg_pong(self.ver_send))

		elif message.command == "addr":
			peermgr.new_addrs(message.addrs)

		elif message.command == "inv":

			# special message sent to kick getblocks
			if (len(message.inv) == 1 and
			    message.inv[0].type == MSG_BLOCK and
			    self.chaindb.haveblock(message.inv[0].hash, True)):
				self.send_getblocks(False)
				return

			want = msg_getdata(self.ver_send)
			for i in message.inv:
				if i.type == 1:
					want.inv.append(i)
				elif i.type == 2:
					want.inv.append(i)
			if len(want.inv):
				self.send_message(want)

		elif message.command == "tx":
			if self.chaindb.tx_is_orphan(message.tx):
				self.log.write("MemPool: Ignoring orphan TX %064x" % (message.tx.sha256,))
			elif not self.chaindb.tx_signed(message.tx, None, True):
				self.log.write("MemPool: Ignoring failed-sig TX %064x" % (message.tx.sha256,))
			else:
				self.mempool.add(message.tx)

		elif message.command == "block":
			self.chaindb.putblock(message.block)
			self.last_block_rx = time.time()

		elif message.command == "getdata":
			self.getdata(message)

		elif message.command == "getblocks":
			self.getblocks(message)

		elif message.command == "getheaders":
			self.getheaders(message)

		elif message.command == "getaddr":
			msg = msg_addr()
			msg.addrs = peermgr.random_addrs()

			self.send_message(msg)

		elif message.command == "mempool":
			msg = msg_inv()
			for k in self.mempool.pool.iterkeys():
				inv = CInv()
				inv.type = MSG_TX
				inv.hash = k
				msg.inv.append(inv)

				if len(msg.inv) == 50000:
					break

			self.send_message(msg)

		# if we haven't seen a 'block' message in a little while,
		# and we're still not caught up, send another getblocks
		last_blkmsg = time.time() - self.last_block_rx
		if last_blkmsg > 5:
			self.send_getblocks()

	def getdata_tx(self, txhash):
		if txhash in self.mempool.pool:
			tx = self.mempool.pool[txhash]
		else:
			tx = self.chaindb.gettx(txhash)
			if tx is None:
				return

		msg = msg_tx()
		msg.tx = tx

		self.send_message(msg)

	def getdata_block(self, blkhash):
		block = self.chaindb.getblock(blkhash)
		if block is None:
			return

		msg = msg_block()
		msg.block = block

		self.send_message(msg)

		if blkhash == self.hash_continue:
			self.hash_continue = None

			inv = CInv()
			inv.type = MSG_BLOCK
			inv.hash = self.chaindb.gettophash()

			msg = msg_inv()
			msg.inv.append(inv)

			self.send_message(msg)

	def getdata(self, message):
		if len(message.inv) > 50000:
			self.handle_close()
			return
		for inv in message.inv:
			if inv.type == MSG_TX:
				self.getdata_tx(inv.hash)
			elif inv.type == MSG_BLOCK:
				self.getdata_block(inv.hash)

	def getblocks(self, message):
		blkmeta = self.chaindb.locate(message.locator)
		height = blkmeta.height
		top_height = self.getheight()
		end_height = height + 500
		if end_height > top_height:
			end_height = top_height

		msg = msg_inv()
		while height <= end_height:
			hash = long(self.chaindb.height[str(height)])
			if hash == message.hashstop:
				break

			inv = CInv()
			inv.type = MSG_BLOCK
			inv.hash = hash
			msg.inv.append(inv)

			height += 1

		if len(msg.inv) > 0:
			self.send_message(msg)
			if height <= top_height:
				self.hash_continue = msg.inv[-1].hash

	def getheaders(self, message):
		blkmeta = self.chaindb.locate(message.locator)
		height = blkmeta.height
		top_height = self.getheight()
		end_height = height + 2000
		if end_height > top_height:
			end_height = top_height

		msg = msg_headers()
		while height <= end_height:
			blkhash = long(self.chaindb.height[str(height)])
			if blkhash == message.hashstop:
				break

			db_block = self.chaindb.getblock(blkhash)
			block = copy.copy(db_block)
			block.vtx = []

			msg.headers.append(block)

			height += 1

		self.send_message(msg)


class PeerManager(object):
	def __init__(self, log, mempool, chaindb, netmagic):
		self.log = log
		self.mempool = mempool
		self.chaindb = chaindb
		self.netmagic = netmagic
		self.peers = []
		self.addrs = {}
		self.tried = {}

	def add(self, host, port):
		self.log.write("PeerManager: connecting to %s:%d" %
			       (host, port))
		self.tried[host] = True
		c = NodeConn(host, port, self.log, self, self.mempool,
			     self.chaindb, self.netmagic)
		self.peers.append(c)
		return c

	def new_addrs(self, addrs):
		for addr in addrs:
			if addr.ip in self.addrs:
				continue
			self.addrs[addr.ip] = addr

		self.log.write("PeerManager: Received %d new addresses (%d addrs, %d tried)" %
				(len(addrs), len(self.addrs),
				 len(self.tried)))

	def random_addrs(self):
		ips = self.addrs.keys()
		random.shuffle(ips)
		if len(ips) > 1000:
			del ips[1000:]

		vaddr = []
		for ip in ips:
			vaddr.append(self.addrs[ip])

		return vaddr

	def closeall(self):
		for peer in self.peers:
			peer.handle_close()
		self.peers = []


if __name__ == '__main__':
	if len(sys.argv) != 2:
		print("Usage: node.py CONFIG-FILE")
		sys.exit(1)

	f = open(sys.argv[1])
	for line in f:
		m = re.search('^(\w+)\s*=\s*(\S.*)$', line)
		if m is None:
			continue
		settings[m.group(1)] = m.group(2)
	f.close()

	if 'host' not in settings:
		settings['host'] = '127.0.0.1'
	if 'port' not in settings:
		settings['port'] = 8333
	if 'rpcport' not in settings:
		settings['rpcport'] = 9332
	if 'db' not in settings:
		settings['db'] = '/tmp/chaindb'
	if 'chain' not in settings:
		settings['chain'] = 'mainnet'
	chain = settings['chain']
	if 'log' not in settings or (settings['log'] == '-'):
		settings['log'] = None

	if ('rpcuser' not in settings or
	    'rpcpass' not in settings):
		print("You must set the following in config: rpcuser, rpcpass")
		sys.exit(1)

	settings['port'] = int(settings['port'])
	settings['rpcport'] = int(settings['rpcport'])

	log = Log.Log(settings['log'])

	log.write("\n\n\n\n")

	if chain not in NETWORKS:
		log.write("invalid network")
		sys.exit(1)

	netmagic = NETWORKS[chain]

	mempool = MemPool.MemPool(log)
	chaindb = ChainDb.ChainDb(settings, settings['db'], log, mempool,
				  netmagic, False, False)
	peermgr = PeerManager(log, mempool, chaindb, netmagic)

	if 'loadblock' in settings:
		chaindb.loadfile(settings['loadblock'])

	threads = []

	# start HTTP server for JSON-RPC
	rpcexec = rpc.RPCExec(peermgr, mempool, chaindb, log,
				  settings['rpcuser'], settings['rpcpass'])
	rpcserver = gevent.pywsgi.WSGIServer(('', settings['rpcport']), rpcexec.handle_request)
	t = gevent.Greenlet(rpcserver.serve_forever)
	threads.append(t)

	# connect to specified remote node
	c = peermgr.add(settings['host'], settings['port'])
	threads.append(c)
	
	if 'addnodes' in settings and settings['addnodes']:
                for node in settings['addnodes'].split():
                        c = peermgr.add(node, settings['port'])
                        threads.append(c)
                        time.sleep(2)

	# program main loop
	def start(timeout=None):
		for t in threads: t.start()
		try:
			gevent.joinall(threads,timeout=timeout,
				       raise_error=True)
		finally:
			for t in threads: t.kill()
			gevent.joinall(threads)
			log.write('Flushing database...')
			del chaindb.db
			chaindb.blk_write.close()
			log.write('OK')

	start()

