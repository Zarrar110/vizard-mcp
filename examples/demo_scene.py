"""A small Vizard scene for trying vizard-mcp.

Run it directly in Vizard (it starts vizmcp itself), or through the
launch_app tool.

- A turntable spins at a set rate. SPACE pauses or resumes it.
- A ball is dropped from 2 m under gravity and bounces with a coefficient
  of restitution of 0.8, so each bounce peaks at 0.8^2 = 0.64 of the last.
  R (or the on-screen Reset button) drops it again.
"""

import os
import sys

import viz
import vizact
import vizshape

# Find vizmcp when this example runs from the repository.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', 'src', 'vizard_mcp', 'vizard_side'))
import vizmcp

viz.setMultiSample(4)
viz.go()
vizmcp.start()

viz.clearcolor([0.62, 0.74, 0.86])
viz.MainView.setPosition([0, 2.2, -7])
viz.MainView.lookAt([0, 0.8, 0])

ground = vizshape.addPlane(size=[20, 20])
ground.color([0.35, 0.38, 0.33])

turntable = viz.addGroup()
base = vizshape.addCylinder(height=0.2, radius=1.6, parent=turntable)
base.setPosition([0, 0.1, 0])
base.color([0.25, 0.25, 0.28])

column = vizshape.addBox(size=[0.4, 1.6, 0.4], parent=turntable)
column.setPosition([0, 1.0, 0])
column.color([0.8, 0.55, 0.2])

beam = vizshape.addBox(size=[2.6, 0.2, 0.3], parent=turntable)
beam.setPosition([0, 1.9, 0])
beam.color([0.2, 0.45, 0.8])

BALL_R = 0.3
ball = vizshape.addSphere(radius=BALL_R)
ball.color([0.85, 0.2, 0.2])

label = viz.addText3D('vizard-mcp demo', pos=[0, 2.6, 0], align=viz.ALIGN_CENTER_BOTTOM)
label.setScale([0.3] * 3)


class Turntable(object):
	def __init__(self):
		self.angle = 0.0
		self.rate_deg_s = 30.0
		self.running = True

	def update(self, dt):
		if self.running:
			self.angle = (self.angle + self.rate_deg_s * dt) % 360.0
		turntable.setEuler([self.angle, 0, 0])

	def toggle(self):
		self.running = not self.running
		print('turntable running = %s' % self.running)


class Ball(object):
	"""Point mass under gravity with a restitution bounce on the ground plane."""

	G = 9.81
	RESTITUTION = 0.8
	DROP_HEIGHT = 2.0     # height of the ball's bottom above the ground, m

	def __init__(self):
		self.reset()

	def reset(self):
		self.height = self.DROP_HEIGHT
		self.velocity = 0.0
		self.bounces = 0
		self.drops = getattr(self, 'drops', 0) + 1
		print('ball dropped (#%d)' % self.drops)

	def update(self, dt):
		# Sub-steps keep the bounce height error well under 1 mm at 60 fps.
		n = 8
		h = dt / n
		for _ in range(n):
			self.velocity -= self.G * h
			self.height += self.velocity * h
			if self.height < 0.0 and self.velocity < 0.0:
				self.height = -self.height * self.RESTITUTION
				self.velocity = -self.velocity * self.RESTITUTION
				self.bounces += 1
		ball.setPosition([2.5, self.height + BALL_R, 0])


table = Turntable()
drop = Ball()


def update():
	dt = min(viz.getFrameElapsed(), 0.05)
	table.update(dt)
	drop.update(dt)


vizact.onupdate(0, update)
vizact.onkeydown(' ', table.toggle)
vizact.onkeydown('r', drop.reset)

reset_button = viz.addButtonLabel('Reset ball')
reset_button.setPosition(0.1, 0.92)
vizact.onbuttondown(reset_button, drop.reset)

print('demo scene ready')
